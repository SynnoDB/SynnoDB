#pragma once

#include <cstdint>
#include <dlfcn.h>
#include <elf.h>
#include <fcntl.h>
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <string>
#include <unistd.h>
#include <vector>

#include "build_id.hpp"

namespace detail {

constexpr bool DEBUG = false;

class PluginLoader {
public:
    static std::filesystem::path resolve_source(const std::string& src) {
        std::filesystem::path p(src);
        if (p.is_absolute())
            return p;
        if (std::filesystem::exists(p))
            return p;
        std::error_code ec;
        std::filesystem::path exe = std::filesystem::read_symlink("/proc/self/exe", ec);
        if (!ec) {
            std::filesystem::path candidate = exe.parent_path() / p;
            if (std::filesystem::exists(candidate))
                return candidate;
        }
        return p;
    }

    static std::string build_id_from_file(const std::string& path) {
        auto resolved = resolve_source(path);
        int fd = ::open(resolved.c_str(), O_RDONLY);
        if (fd < 0)
            return {};

        Elf64_Ehdr eh{};
        if (read(fd, &eh, sizeof(eh)) != sizeof(eh)) {
            ::close(fd);
            return {};
        }
        if (eh.e_ident[EI_MAG0] != ELFMAG0 || eh.e_ident[EI_MAG1] != ELFMAG1 ||
            eh.e_ident[EI_MAG2] != ELFMAG2 || eh.e_ident[EI_MAG3] != ELFMAG3) {
            ::close(fd);
            return {};
        }
        if (eh.e_phoff == 0 || eh.e_phnum == 0) {
            ::close(fd);
            return {};
        }

        std::vector<Elf64_Phdr> phdrs(eh.e_phnum);
        if (lseek(fd, static_cast<off_t>(eh.e_phoff), SEEK_SET) < 0) {
            ::close(fd);
            return {};
        }
        ssize_t phdr_bytes = static_cast<ssize_t>(eh.e_phnum * sizeof(Elf64_Phdr));
        if (read(fd, phdrs.data(), phdr_bytes) != phdr_bytes) {
            ::close(fd);
            return {};
        }

        std::string out;
        for (const auto& ph : phdrs) {
            if (ph.p_type != PT_NOTE || ph.p_filesz == 0)
                continue;
            std::vector<unsigned char> buf(ph.p_filesz);
            if (lseek(fd, static_cast<off_t>(ph.p_offset), SEEK_SET) < 0)
                continue;
            if (read(fd, buf.data(), static_cast<ssize_t>(buf.size())) !=
                static_cast<ssize_t>(buf.size()))
                continue;

            size_t off = 0;
            while (off + sizeof(Elf64_Nhdr) <= buf.size()) {
                const Elf64_Nhdr* nh =
                    reinterpret_cast<const Elf64_Nhdr*>(buf.data() + off);
                off += sizeof(*nh);
                if (off > buf.size())
                    break;

                const char* name = reinterpret_cast<const char*>(buf.data() + off);
                off += (nh->n_namesz + 3) & ~3U;
                if (off > buf.size())
                    break;

                const unsigned char* desc = buf.data() + off;
                off += (nh->n_descsz + 3) & ~3U;
                if (off > buf.size())
                    break;

                if (nh->n_type == NT_GNU_BUILD_ID && nh->n_namesz >= 3 &&
                    std::string(name, 3) == "GNU") {
                    static const char hex[] = "0123456789abcdef";
                    out.reserve(nh->n_descsz * 2);
                    for (size_t i = 0; i < nh->n_descsz; ++i) {
                        out.push_back(hex[(desc[i] >> 4) & 0xF]);
                        out.push_back(hex[desc[i] & 0xF]);
                    }
                    ::close(fd);
                    return out;
                }
            }
        }

        ::close(fd);
        return {};
    }

    static std::string make_loaded_path(const std::string& src) {
        std::filesystem::path p(src);
        std::filesystem::path dir = p.parent_path() / ".reload";
        std::filesystem::create_directories(dir);
        static int counter = 0;
        std::filesystem::path out = dir / (p.filename().string() + "." +
                                           std::to_string(::getpid()) + "." +
                                           std::to_string(++counter) + ".so");
        return out.string();
    }

    static std::string copy_to_reload(const std::string& src) {
        std::filesystem::path resolved = resolve_source(src);
        std::string out = make_loaded_path(resolved.string());
        try {
            if constexpr (DEBUG) {
                std::cerr << "Plugin load: source=" << src
                          << " resolved=" << resolved.string()
                          << " exists=" << std::filesystem::exists(resolved)
                          << " dest=" << out
                          << " cwd=" << std::filesystem::current_path().string()
                          << "\n";
            }
            std::filesystem::copy_file(
                resolved,
                out,
                std::filesystem::copy_options::overwrite_existing
            );
        } catch (const std::filesystem::filesystem_error& ex) {
            if constexpr (DEBUG) {
                std::cerr << "Plugin load: copy_file failed: " << ex.what()
                          << " source=" << src
                          << " resolved=" << resolved.string()
                          << " dest=" << out
                          << " cwd=" << std::filesystem::current_path().string()
                          << "\n";
            }
            throw;
        }
        return out;
    }

    static std::string display_path(const std::string& path) {
        std::filesystem::path cwd = std::filesystem::current_path();
        std::filesystem::path p(path);
        std::error_code ec;
        auto rel = std::filesystem::relative(p, cwd, ec);
        if (!ec)
            return rel.string();
        return path;
    }
};

} // namespace detail

class Plugin {
public:
    explicit Plugin(const std::string& path) {
        source_path_ = path;
        load_fresh();

        query_ = (const void* (*)())dlsym(h_, "plugin_query");
        if (!query_)
            throw std::runtime_error(std::string("dlsym: ") + dlerror());

    }

    Plugin(const Plugin&) = delete;
    Plugin& operator=(const Plugin&) = delete;

    Plugin(Plugin&& o) noexcept { *this = std::move(o); }
    Plugin& operator=(Plugin&& o) noexcept {
        close();
        h_ = o.h_;
        query_ = o.query_;
        source_path_ = std::move(o.source_path_);
        loaded_path_ = std::move(o.loaded_path_);
        o.h_ = nullptr;
        o.query_ = nullptr;
        return *this;
    }

    ~Plugin() { close(); }

    template <class T>
    T get() const {
        const auto* api = static_cast<const T*>(query_());
        if (!api)
            throw std::runtime_error("plugin_query returned null");
        return *api;
    }

    const char* build_id() const { return build_id::for_address((void*)query_); }

    std::string file_build_id() const {
        return detail::PluginLoader::build_id_from_file(source_path_);
    }

    static std::string file_build_id_for(const std::string& path) {
        return detail::PluginLoader::build_id_from_file(path);
    }

    bool needs_reload() const {
        std::string file_id = file_build_id();
        const char* loaded = build_id();
        std::string loaded_id = loaded ? loaded : "";
        if (file_id.empty() || loaded_id.empty())
            return false;
        return file_id != loaded_id;
    }

    void reload() {
        load_fresh();
        query_ = (const void* (*)())dlsym(h_, "plugin_query");
        if (!query_)
            throw std::runtime_error(std::string("dlsym: ") + dlerror());
    }

private:
    void load_fresh() {
        close();
        loaded_path_ = detail::PluginLoader::copy_to_reload(source_path_);
        h_ = dlopen(loaded_path_.c_str(), RTLD_NOW);
        if (!h_)
            throw std::runtime_error(std::string("dlopen: ") + dlerror());
        if constexpr (detail::DEBUG) {
            std::cerr << "Plugin load: " << loaded_path_ << "\n";
        } else {
            std::cerr << "Plugin load: " << detail::PluginLoader::display_path(loaded_path_) << "\n";
        }
    }
    void close() noexcept {
        if (h_)
            dlclose(h_);
        h_ = nullptr;
        query_ = nullptr;
        if (!loaded_path_.empty()) {
            std::error_code ec;
            std::filesystem::remove(loaded_path_, ec);
            loaded_path_.clear();
        }
    }

    void* h_ = nullptr;
    const void* (*query_)() = nullptr;
    std::string source_path_;
    std::string loaded_path_;
};
