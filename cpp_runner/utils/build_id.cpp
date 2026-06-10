#include "build_id.hpp"

#include <cstring>
#include <elf.h>
#include <link.h>
#include <string>

namespace build_id {

const char *for_address(const void *addr) {
    thread_local std::string out;
    out.clear();

    Dl_info sym{};
    if (dladdr(const_cast<void *>(addr), &sym) == 0 || !sym.dli_fname) {
        return out.c_str();
    }
    const char *my_path = sym.dli_fname;

    dl_iterate_phdr(
        [](dl_phdr_info *info, size_t, void *data) -> int {
            const char *want = static_cast<const char *>(data);

            if (!info->dlpi_name || std::strcmp(info->dlpi_name, want) != 0) {
                return 0;
            }

            for (int i = 0; i < info->dlpi_phnum; ++i) {
                const ElfW(Phdr) &phdr = info->dlpi_phdr[i];
                if (phdr.p_type != PT_NOTE)
                    continue;

                const unsigned char *p =
                    reinterpret_cast<const unsigned char *>(info->dlpi_addr + phdr.p_vaddr);
                const unsigned char *end = p + phdr.p_memsz;

                while (p + sizeof(ElfW(Nhdr)) <= end) {
                    const ElfW(Nhdr) *nh = reinterpret_cast<const ElfW(Nhdr) *>(p);
                    p += sizeof(*nh);

                    const char *name = reinterpret_cast<const char *>(p);
                    p += ((nh->n_namesz + 3) & ~3);

                    const unsigned char *desc = p;
                    p += ((nh->n_descsz + 3) & ~3);

                    if (nh->n_type == NT_GNU_BUILD_ID && std::strcmp(name, "GNU") == 0) {
                        static const char hex[] = "0123456789abcdef";
                        size_t len = nh->n_descsz;
                        out.reserve(len * 2);
                        for (size_t j = 0; j < len; ++j) {
                            out.push_back(hex[(desc[j] >> 4) & 0xF]);
                            out.push_back(hex[desc[j] & 0xF]);
                        }
                        return 1;
                    }
                }
            }
            return 1;
        },
        (void *)my_path);

    return out.c_str();
}

} // namespace build_id
