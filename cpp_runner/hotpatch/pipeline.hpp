#pragma once

#include "plugin.hpp"

#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <poll.h>
#include <signal.h>
#include <stdexcept>
#include <string>
#include <sys/prctl.h>
#include <sys/signalfd.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <type_traits>
#include <unistd.h>
#include <utility>
#include <vector>

// FILE_VERSION: 1

enum class Action {
    RUN,
    TERMINATE,
};

// Carries the per-invocation payload from Python → run_parent → pipeline stages.
// batch_id is a monotonically-increasing counter assigned by the Python side; the
// C++ side echoes it back in the JSON response so the caller can detect any
// mismatch caused by stale pipe data.  query_lines holds the raw query strings
// that were previously sent over stdin line-by-line; run_env holds environment
// overrides that should apply to this specific invocation. Bundling them here
// ensures they are always in sync with the RUN command and cannot bleed into a
// later run.
struct RunBatch {
    uint64_t batch_id = 0;
    std::vector<std::string> query_lines;
    std::vector<std::pair<std::string, std::string>> run_env;
};

namespace ipc {

struct io_error : std::runtime_error {
    using std::runtime_error::runtime_error;
};

template <class T>
void read_exact(int fd, T& out) {
    static_assert(std::is_trivially_copyable_v<T>, "T must be trivially copyable");
    static_assert(std::is_standard_layout_v<T>, "T must be standard layout");

    std::byte* p = reinterpret_cast<std::byte*>(&out);
    size_t off = 0;

    while (off < sizeof(T)) {
        ssize_t r = ::read(fd, p + off, sizeof(T) - off);
        if (r > 0) {
            off += static_cast<size_t>(r);
        } else if (r == 0) {
            throw io_error("read_exact: unexpected EOF");
        } else if (errno != EINTR) {
            throw io_error(std::string("read_exact: ") + std::strerror(errno));
        }
    }
}

template <class T>
void write_exact(int fd, const T& value) {
    static_assert(std::is_trivially_copyable_v<T>, "T must be trivially copyable");
    static_assert(std::is_standard_layout_v<T>, "T must be standard layout");

    const std::byte* p = reinterpret_cast<const std::byte*>(&value);
    size_t off = 0;

    while (off < sizeof(T)) {
        ssize_t w = ::write(fd, p + off, sizeof(T) - off);
        if (w > 0) {
            off += static_cast<size_t>(w);
        } else if (w == 0) {
            throw io_error("write_exact: wrote 0 bytes");
        } else if (errno != EINTR) {
            throw io_error(std::string("write_exact: ") + std::strerror(errno));
        }
    }
}

// Framed binary protocol replacing the old text-based "run\n" / "stop\n" stdin
// protocol.  The magic word guards against misaligned reads if the pipe ever
// carries unexpected data.  Wire layout (little-endian, no padding):
//   [uint32 magic][uint32 action][uint64 batch_id][uint32 line_count][uint32 env_count]
//   followed by line_count × [uint32 len][utf-8 bytes]
//   then env_count × key/value string pairs.
// TERMINATE messages always carry line_count == 0 and env_count == 0.
constexpr uint32_t MESSAGE_MAGIC = 0x31525043; // "CPR1" little-endian
constexpr uint32_t ACTION_RUN = 1;
constexpr uint32_t ACTION_TERMINATE = 2;

struct ControlMessage {
    Action action = Action::RUN;
    RunBatch batch;
};

inline void write_bytes(int fd, const char* data, size_t len) {
    size_t off = 0;
    while (off < len) {
        ssize_t w = ::write(fd, data + off, len - off);
        if (w > 0) {
            off += static_cast<size_t>(w);
        } else if (w == 0) {
            throw io_error("write_bytes: wrote 0 bytes");
        } else if (errno != EINTR) {
            throw io_error(std::string("write_bytes: ") + std::strerror(errno));
        }
    }
}

inline void read_bytes(int fd, char* data, size_t len) {
    size_t off = 0;
    while (off < len) {
        ssize_t r = ::read(fd, data + off, len - off);
        if (r > 0) {
            off += static_cast<size_t>(r);
        } else if (r == 0) {
            throw io_error("read_bytes: unexpected EOF");
        } else if (errno != EINTR) {
            throw io_error(std::string("read_bytes: ") + std::strerror(errno));
        }
    }
}

inline void write_string(int fd, const std::string& value) {
    if (value.size() > UINT32_MAX) {
        throw io_error("write_string: string too large");
    }
    uint32_t len = static_cast<uint32_t>(value.size());
    write_exact(fd, len);
    write_bytes(fd, value.data(), value.size());
}

inline std::string read_string(int fd) {
    uint32_t len = 0;
    read_exact(fd, len);
    std::string value(len, '\0');
    if (len > 0) {
        read_bytes(fd, value.data(), len);
    }
    return value;
}

inline void write_control_message(int fd, const ControlMessage& msg) {
    uint32_t magic = MESSAGE_MAGIC;
    uint32_t action =
        msg.action == Action::RUN ? ACTION_RUN : ACTION_TERMINATE;
    uint64_t batch_id = msg.batch.batch_id;
    if (msg.batch.query_lines.size() > UINT32_MAX) {
        throw io_error("write_control_message: too many query lines");
    }
    if (msg.batch.run_env.size() > UINT32_MAX) {
        throw io_error("write_control_message: too many environment variables");
    }
    uint32_t line_count =
        msg.action == Action::RUN
            ? static_cast<uint32_t>(msg.batch.query_lines.size())
            : 0;
    uint32_t env_count =
        msg.action == Action::RUN
            ? static_cast<uint32_t>(msg.batch.run_env.size())
            : 0;

    write_exact(fd, magic);
    write_exact(fd, action);
    write_exact(fd, batch_id);
    write_exact(fd, line_count);
    write_exact(fd, env_count);
    for (const auto& line : msg.batch.query_lines) {
        write_string(fd, line);
    }
    for (const auto& [key, value] : msg.batch.run_env) {
        write_string(fd, key);
        write_string(fd, value);
    }
}

inline ControlMessage read_control_message(int fd) {
    uint32_t magic = 0;
    uint32_t action = 0;
    uint64_t batch_id = 0;
    uint32_t line_count = 0;
    uint32_t env_count = 0;

    read_exact(fd, magic);
    if (magic != MESSAGE_MAGIC) {
        throw io_error("read_control_message: invalid message magic");
    }
    read_exact(fd, action);
    read_exact(fd, batch_id);
    read_exact(fd, line_count);
    read_exact(fd, env_count);

    ControlMessage msg;
    if (action == ACTION_RUN) {
        msg.action = Action::RUN;
    } else if (action == ACTION_TERMINATE) {
        msg.action = Action::TERMINATE;
    } else {
        throw io_error("read_control_message: invalid action");
    }
    msg.batch.batch_id = batch_id;

    if (msg.action == Action::RUN) {
        msg.batch.query_lines.reserve(line_count);
        for (uint32_t i = 0; i < line_count; ++i) {
            msg.batch.query_lines.push_back(read_string(fd));
        }
        msg.batch.run_env.reserve(env_count);
        for (uint32_t i = 0; i < env_count; ++i) {
            msg.batch.run_env.emplace_back(read_string(fd), read_string(fd));
        }
    } else if (line_count != 0 || env_count != 0) {
        throw io_error("read_control_message: terminate message has payload");
    }
    return msg;
}

} // namespace ipc

struct DoneToken {
    int exit_code = 0;
    int term_signal = 0;
};

// Sentinel exit code written to the done pipe when a stage's plugin callback
// throws a C++ exception that the stage runner caught (see stage_loop_impl).
// It lets run_parent distinguish a caught-exception failure — which carries no
// trace payload — from a normal exit_code-0 completion, without abusing the
// term_signal field (no signal was actually raised). 70 == EX_SOFTWARE.
inline constexpr int kStageThrewExitCode = 70;

enum class RunPolicy {
    OnChange,
    Always,
    AlwaysReload,  // like Always, but also forces dlclose/dlopen on every run
};


namespace detail {

class RunEnvGuard {
private:
    struct PreviousEnv {
        std::string key;
        bool had_value = false;
        std::string value;
    };

public:
    explicit RunEnvGuard(const RunBatch& batch) {
        previous_.reserve(batch.run_env.size());
        for (const auto& [key, value] : batch.run_env) {
            if (key.empty() || key.find('=') != std::string::npos) {
                throw std::runtime_error("invalid run_env key: " + key);
            }
            const char* old_value = ::getenv(key.c_str());
            previous_.push_back(PreviousEnv{
                key,
                old_value != nullptr,
                old_value != nullptr ? std::string(old_value) : std::string{},
            });
            if (::setenv(key.c_str(), value.c_str(), 1) != 0) {
                throw std::runtime_error("setenv failed for run_env key: " + key);
            }
        }
    }

    RunEnvGuard(const RunEnvGuard&) = delete;
    RunEnvGuard& operator=(const RunEnvGuard&) = delete;

    ~RunEnvGuard() {
        for (auto it = previous_.rbegin(); it != previous_.rend(); ++it) {
            if (it->had_value) {
                ::setenv(it->key.c_str(), it->value.c_str(), 1);
            } else {
                ::unsetenv(it->key.c_str());
            }
        }
    }

private:
    std::vector<PreviousEnv> previous_;
};

struct ChildHandle {
    pid_t pid = -1;
    int write_fd = -1;
    int pid_fd = -1;
};

template <class T>
struct compute_traits;

template <class C, class R, class Arg0>
struct compute_traits<R (C::*)(Arg0) const> {
    using input_type = void;
};

template <class C, class R, class Arg0, class Arg1>
struct compute_traits<R (C::*)(Arg0, Arg1) const> {
    using input_type = Arg1;
};

// Three-argument overload: (Plugin&, Input, const RunBatch&).
// input_type is still Arg1 — the RunBatch is not treated as the stage input.
template <class C, class R, class Arg0, class Arg1, class Arg2>
struct compute_traits<R (C::*)(Arg0, Arg1, Arg2) const> {
    using input_type = Arg1;
};

template <class Compute>
using compute_input_t = typename compute_traits<decltype(&Compute::operator())>::input_type;

struct NoInput {};

// Deduce the return type of a stage compute lambda, handling both the
// two-argument form (Plugin&, Input) and the batch-aware three-argument form
// (Plugin&, Input, const RunBatch&).  WithBatch selects the correct specialisation.
template <class Compute, class Input, bool WithBatch>
struct compute_result_type_impl;

template <class Compute, class Input>
struct compute_result_type_impl<Compute, Input, true> {
    using type = std::invoke_result_t<Compute, Plugin&, Input, const RunBatch&>;
};

template <class Compute, class Input>
struct compute_result_type_impl<Compute, Input, false> {
    using type = std::invoke_result_t<Compute, Plugin&, Input>;
};

template <class Compute, class Input>
struct compute_result_type
    : compute_result_type_impl<
          Compute,
          Input,
          std::is_invocable_v<Compute, Plugin&, Input, const RunBatch&>> {};

template <class Compute>
struct compute_result_type<Compute, NoInput> {
    using type = std::invoke_result_t<Compute, Plugin&>;
};

template <class Compute, class Input>
using compute_result_t = typename compute_result_type<Compute, Input>::type;

template <class Compute>
static auto compute_result(Compute& compute, Plugin& plugin, const NoInput&) {
    return compute(plugin);
}

template <class Compute, class Input>
static auto compute_result(Compute& compute, Plugin& plugin, const Input& input) {
    return compute(plugin, input);
}

// Dispatch: if the lambda accepts a RunBatch, forward it; otherwise fall back
// to the two-argument overload so older stage lambdas need no changes.
template <class Compute, class Input>
static auto compute_result(
    Compute& compute,
    Plugin& plugin,
    const Input& input,
    const RunBatch& batch) {
    if constexpr (std::is_invocable_v<Compute, Plugin&, Input, const RunBatch&>) {
        return compute(plugin, input, batch);
    } else {
        return compute_result(compute, plugin, input);
    }
}

template <RunPolicy P, class Input, class Compute, class Teardown = std::nullptr_t>
struct StageDef {
    using input_type = Input;
    static constexpr RunPolicy policy = P;
    const char* so_path;
    Compute compute;
    // Called with the old Plugin& before dlclose/dlopen when a reload is triggered.
    // Use to destroy state that was allocated via the old library before it is unmapped.
    Teardown teardown;
};

template <RunPolicy P, class Compute>
static StageDef<P, detail::compute_input_t<Compute>, Compute> make_stage(
    const char* so_path,
    Compute compute) {
    return {so_path, compute, nullptr};
}

template <RunPolicy P, class Compute, class Teardown>
static StageDef<P, detail::compute_input_t<Compute>, Compute, Teardown> make_stage(
    const char* so_path,
    Compute compute,
    Teardown teardown) {
    return {so_path, compute, teardown};
}

static void stop_child(ChildHandle& child) {
    if (child.pid > 0) {
        try {
            ipc::write_control_message(
                child.write_fd,
                ipc::ControlMessage{.action = Action::TERMINATE});
        } catch (const std::exception&) {
        }
        close(child.write_fd);
        waitpid(child.pid, nullptr, 0);
    }
    if (child.pid_fd >= 0) {
        close(child.pid_fd);
        child.pid_fd = -1;
    }
    child.pid = -1;
    child.write_fd = -1;
}

struct StatusInfo {
    int exit_code = 0;
    int term_signal = 0;
};

static StatusInfo status_to_info(int status) {
    StatusInfo info{};
    if (WIFSIGNALED(status)) {
        info.term_signal = WTERMSIG(status);
        return info;
    }
    if (WIFEXITED(status)) {
        info.exit_code = WEXITSTATUS(status);
        return info;
    }
    info.exit_code = -1;
    return info;
}

static void write_done(int done_fd, StatusInfo info) {
    if (done_fd < 0)
        return;
    DoneToken token{info.exit_code, info.term_signal};
    ipc::write_exact(done_fd, token);
}

static bool reap_dead_child(ChildHandle& child, int done_fd) {
    if (child.pid <= 0)
        return false;
    int status = 0;
    pid_t r = waitpid(child.pid, &status, WNOHANG);
    if (r <= 0)
        return false;
    write_done(done_fd, status_to_info(status));
    stop_child(child);
    return true;
}

// Propagate the RunBatch (including query_lines) to the child stage so every
// stage in the chain processes exactly the same batch as the one requested.
static bool notify_child_run(ChildHandle& child, int done_fd, const RunBatch& batch) {
    if (child.pid <= 0)
        return false;
    try {
        ipc::write_control_message(
            child.write_fd,
            ipc::ControlMessage{.action = Action::RUN, .batch = batch});
        return true;
    } catch (const std::exception&) {
        int status = 0;
        waitpid(child.pid, &status, 0);
        write_done(done_fd, status_to_info(status));
        stop_child(child);
        return false;
    }
}

static int setup_sigchld_fd() {
    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGCHLD);
    if (sigprocmask(SIG_BLOCK, &mask, nullptr) != 0)
        return -1;
    return signalfd(-1, &mask, SFD_CLOEXEC);
}

static int open_pidfd(pid_t pid) {
#ifdef SYS_pidfd_open
    int fd = static_cast<int>(syscall(SYS_pidfd_open, pid, 0));
    if (fd >= 0)
        return fd;
#endif
    return -1;
}

} // namespace detail

class PipelineControl {
public:
    PipelineControl(int write_fd, int done_fd, bool own_fds = false)
        : write_fd_(write_fd), done_fd_(done_fd), own_fds_(own_fds) {}

    PipelineControl(const PipelineControl&) = delete;
    PipelineControl& operator=(const PipelineControl&) = delete;

    PipelineControl(PipelineControl&& other) noexcept
        : write_fd_(other.write_fd_), done_fd_(other.done_fd_), own_fds_(other.own_fds_) {
        other.write_fd_ = -1;
        other.done_fd_ = -1;
        other.own_fds_ = false;
    }

    PipelineControl& operator=(PipelineControl&& other) noexcept {
        if (this != &other) {
            close();
            write_fd_ = other.write_fd_;
            done_fd_ = other.done_fd_;
            own_fds_ = other.own_fds_;
            other.write_fd_ = -1;
            other.done_fd_ = -1;
            other.own_fds_ = false;
        }
        return *this;
    }

    ~PipelineControl() { close(); }

    void send_run(const RunBatch& batch) const {
        ipc::write_control_message(
            write_fd_,
            ipc::ControlMessage{.action = Action::RUN, .batch = batch});
    }

    void send_terminate() const {
        ipc::write_control_message(
            write_fd_,
            ipc::ControlMessage{.action = Action::TERMINATE});
    }

    DoneToken read_done() const {
        DoneToken token{};
        ipc::read_exact(done_fd_, token);
        return token;
    }

    int done_fd() const {
        return done_fd_;
    }

    void close() noexcept {
        if (!own_fds_)
            return;
        if (write_fd_ >= 0) {
            ::close(write_fd_);
            write_fd_ = -1;
        }
        if (done_fd_ >= 0) {
            ::close(done_fd_);
            done_fd_ = -1;
        }
        own_fds_ = false;
    }

private:
    int write_fd_ = -1;
    int done_fd_ = -1;
    bool own_fds_ = false;
};

template <RunPolicy P, class Compute>
static auto stage(const char* so_path, Compute compute) {
    return detail::make_stage<P>(so_path, compute);
}

template <RunPolicy P, class Compute, class Teardown>
static auto stage(const char* so_path, Compute compute, Teardown teardown) {
    return detail::make_stage<P>(so_path, compute, teardown);
}

template <RunPolicy P, bool Done, class Input, class Compute, class Teardown, class NextStart>
static void stage_loop_impl(
    int read_fd,
    int done_fd,
    const char* so_path,
    Input input,
    Compute compute,
    Teardown teardown,
    NextStart next_start,
    bool start_now) {
    Plugin plugin(so_path);
    using Output = detail::compute_result_t<Compute, Input>;
    if constexpr (Done) {
        static_assert(std::is_convertible_v<Output, int>,
                      "DoneToken requires last stage output convertible to int");
    }
    Output result{};
    bool has_run = false;
    detail::ChildHandle child;
    bool child_active = false;
    int sigfd = -1;
    if constexpr (!std::is_same_v<NextStart, std::nullptr_t>) {
        sigfd = detail::setup_sigchld_fd();
        if (sigfd < 0) {
            throw std::runtime_error("setup_sigchld_fd failed");
        }
    }

    auto do_run = [&](const RunBatch& batch) {
        detail::RunEnvGuard run_env_guard(batch);
        bool reload = plugin.needs_reload() || P == RunPolicy::AlwaysReload;
        bool should_run = reload || P == RunPolicy::Always || !has_run;
        if (reload) {
            if constexpr (!std::is_same_v<NextStart, std::nullptr_t>) {
                if (child_active) {
                    detail::stop_child(child);
                    child_active = false;
                }
            }
            if constexpr (!std::is_same_v<Teardown, std::nullptr_t>) {
                // A throwing teardown must not abort the process: log it and
                // still proceed with the reload so a fixed plugin can load.
                try {
                    teardown(plugin);
                } catch (const std::exception& e) {
                    std::cerr << so_path << " teardown threw std::exception: "
                              << e.what() << " (continuing reload)\n";
                } catch (...) {
                    std::cerr << so_path
                              << " teardown threw unknown exception"
                                 " (continuing reload)\n";
                }
            }
            plugin.reload();
        }
        if (should_run) {
            // Run the plugin's compute callback under a guard. An uncaught C++
            // exception here would otherwise unwind out of the stage process and
            // trip std::terminate()/SIGABRT, taking the stage down hard and
            // forcing a full restart. Instead we catch it, surface it, report a
            // failure to run_parent, and keep this process alive so a later
            // (post-hotpatch) RUN can retry. Note: this does NOT catch async
            // signals (SIGSEGV, etc.); those still kill the child and are
            // reported via term_signal when the parent reaps it.
            std::string stage_error;
            try {
                result = detail::compute_result(compute, plugin, input, batch);
            } catch (const std::exception& e) {
                stage_error = std::string(so_path)
                            + " stage threw std::exception: " + e.what();
            } catch (...) {
                stage_error = std::string(so_path)
                            + " stage threw unknown exception";
            }
            if (!stage_error.empty()) {
                std::cerr << stage_error << "\n";
                if constexpr (!std::is_same_v<NextStart, std::nullptr_t>) {
                    if (child_active) {
                        detail::stop_child(child);
                        child_active = false;
                    }
                }
                // Report the failure on the done pipe so run_parent unblocks and
                // emits a structured error. Skip starting/notifying downstream
                // stages — the input they would consume was never produced.
                detail::write_done(done_fd,
                                   detail::StatusInfo{kStageThrewExitCode, 0});
                return;
            }
            has_run = true;
            if constexpr (!std::is_same_v<NextStart, std::nullptr_t>) {
                child = next_start(result, read_fd);
                child_active = true;
            }
        } else if constexpr (!std::is_same_v<NextStart, std::nullptr_t>) {
            if (has_run && !child_active) {
                child = next_start(result, read_fd);
                child_active = true;
            }
        }
        if constexpr (Done) {
            detail::write_done(done_fd, detail::StatusInfo{static_cast<int>(result), 0});
        }
        if constexpr (!std::is_same_v<NextStart, std::nullptr_t>) {
            if (child_active)
                detail::notify_child_run(child, done_fd, batch);
        }
    };

    if (start_now) {
        // Startup run before any command arrives (e.g. ingest on first load).
        // No real batch yet, so pass an empty one; batch_id 0 is never echoed
        // back to Python because run_parent has not sent a RUN message yet.
        do_run(RunBatch{});
    }

    if constexpr (!std::is_same_v<NextStart, std::nullptr_t>) {
        while (true) {
            struct pollfd fds[3];
            int nfds = 0;
            fds[nfds].fd = read_fd;
            fds[nfds].events = POLLIN;
            nfds++;
            if (child_active) {
                if (child.pid_fd >= 0) {
                    fds[nfds].fd = child.pid_fd;
                    fds[nfds].events = POLLIN;
                    nfds++;
                } else {
                    fds[nfds].fd = sigfd;
                    fds[nfds].events = POLLIN;
                    nfds++;
                }
            }
            int poll_rc = poll(fds, nfds, -1);
            if (poll_rc < 0) {
                if (errno == EINTR)
                    continue;
                throw ipc::io_error(std::string("poll: ") + std::strerror(errno));
            }
            for (int i = 0; i < nfds; ++i) {
                short re = fds[i].revents;
                if (re == 0)
                    continue;
                // POLLERR / POLLNVAL are unrecoverable: throw rather than
                // looping (poll() would keep waking immediately with the same
                // flags, producing a 100% CPU spin).
                if (re & (POLLERR | POLLNVAL)) {
                    if (child_active)
                        detail::stop_child(child);
                    throw ipc::io_error(
                        "stage_loop: poll returned POLLERR/POLLNVAL");
                }
                if (fds[i].fd == read_fd) {
                    if (re & POLLIN) {
                        ipc::ControlMessage msg = ipc::read_control_message(read_fd);
                        switch (msg.action) {
                            case Action::RUN: {
                                if (child_active && detail::reap_dead_child(child, done_fd)) {
                                    child_active = false;
                                }
                                do_run(msg.batch);
                                if (child_active && detail::reap_dead_child(child, done_fd)) {
                                    child_active = false;
                                }
                                break;
                            }
                            case Action::TERMINATE:
                                if (child_active)
                                    detail::stop_child(child);
                                std::cerr << so_path << " child terminates\n";
                                _exit(0);
                            default:
                                throw std::runtime_error("unknown action");
                        }
                    } else if (re & POLLHUP) {
                        // Control-pipe writer is gone (parent died). Without
                        // this branch the for-loop would skip and poll() would
                        // wake again immediately with POLLHUP set, producing
                        // a 100% CPU busy-loop. Exit cleanly instead.
                        if (child_active)
                            detail::stop_child(child);
                        std::cerr << so_path << " control pipe hung up; exiting\n";
                        _exit(0);
                    }
                    continue;
                }
                if (!(re & POLLIN))
                    continue;
                if (child_active && fds[i].fd == sigfd) {
                    struct signalfd_siginfo info{};
                    ipc::read_exact(sigfd, info);
                    if (detail::reap_dead_child(child, done_fd)) {
                        child_active = false;
                    }
                    continue;
                }
                if (child_active && fds[i].fd == child.pid_fd) {
                    if (detail::reap_dead_child(child, done_fd)) {
                        child_active = false;
                    }
                    continue;
                }
            }
        }
    } else {
        while (true) {
            ipc::ControlMessage msg = ipc::read_control_message(read_fd);
            switch (msg.action) {
                case Action::RUN: {
                    if constexpr (!std::is_same_v<NextStart, std::nullptr_t>) {
                        if (child_active && detail::reap_dead_child(child, done_fd)) {
                            child_active = false;
                        }
                    }
                    do_run(msg.batch);
                    if (child_active && detail::reap_dead_child(child, done_fd)) {
                        child_active = false;
                    }
                    break;
                }
                case Action::TERMINATE:
                    std::cerr << so_path << " child terminates\n";
                    _exit(0);
                default:
                    throw std::runtime_error("unknown action");
            }
        }
    }
}

template <RunPolicy P, class Input, class Compute, class Teardown, class NextStart>
static detail::ChildHandle start_stage_child(
    const char* so_path,
    Input input,
    Compute compute,
    Teardown teardown,
    NextStart next_start,
    int done_fd,
    int close_fd) {
    int pipe_fd[2];
    if (pipe(pipe_fd) == -1) {
        perror("pipe");
        return {};
    }
    pid_t pid = fork();
    if (pid == 0) {
        // Tie this stage child's lifetime to its parent stage process. If the
        // parent dies (e.g. the C++ run_parent terminates because Python was
        // SIGKILL'd), the kernel sends SIGKILL here so we don't leak an
        // orphaned process spinning on a hung-up pipe.
        prctl(PR_SET_PDEATHSIG, SIGKILL);
        close(pipe_fd[1]);
        if (close_fd >= 0)
            close(close_fd);
        stage_loop_impl<P, false>(
            pipe_fd[0],
            done_fd,
            so_path,
            input,
            compute,
            teardown,
            next_start,
            false);
        _exit(0);
    }
    if (pid < 0) {
        perror("fork");
        close(pipe_fd[0]);
        close(pipe_fd[1]);
        return {};
    }
    close(pipe_fd[0]);
    detail::ChildHandle child{pid, pipe_fd[1], -1};
    child.pid_fd = detail::open_pidfd(pid);
    return child;
}

template <class Stage, class Next>
struct Pipeline {
    Stage stage_def;
    Next next;

    template <bool PropagateDone>
    auto make_next_start(int done_fd) {
        if constexpr (std::is_same_v<Next, std::nullptr_t>) {
            return nullptr;
        } else if constexpr (PropagateDone) {
            return [this, done_fd](auto output, int parent_fd) {
                return next.start(output, parent_fd, done_fd);
            };
        } else {
            return [this](auto output, int parent_fd) {
                (void)parent_fd;
                return next.start(output, parent_fd);
            };
        }
    }

    template <bool EmitDone, bool PropagateDone, bool StartNow, class Input>
    void run_impl(int read_fd, int done_fd, Input input) {
        auto next_start = make_next_start<PropagateDone>(done_fd);
        stage_loop_impl<Stage::policy, EmitDone>(
            read_fd,
            done_fd,
            stage_def.so_path,
            input,
            stage_def.compute,
            stage_def.teardown,
            next_start,
            StartNow);
    }

    template <bool EmitDone, bool PropagateDone, class Input>
    detail::ChildHandle start_child_impl(Input input, int done_fd, int close_fd) {
        if constexpr (std::is_same_v<Next, std::nullptr_t>) {
            int pipe_fd[2];
            if (pipe(pipe_fd) == -1) {
                perror("pipe");
                return {};
            }
            pid_t pid = fork();
            if (pid == 0) {
                // See start_stage_child: same rationale for PR_SET_PDEATHSIG.
                prctl(PR_SET_PDEATHSIG, SIGKILL);
                close(pipe_fd[1]);
                if (close_fd >= 0)
                    close(close_fd);
                stage_loop_impl<Stage::policy, EmitDone>(
                    pipe_fd[0],
                    done_fd,
                    stage_def.so_path,
                    input,
                    stage_def.compute,
                    stage_def.teardown,
                    nullptr,
                    false);
                _exit(0);
            }
            if (pid < 0) {
                perror("fork");
                close(pipe_fd[0]);
                close(pipe_fd[1]);
                return {};
            }
            close(pipe_fd[0]);
            detail::ChildHandle child{pid, pipe_fd[1], -1};
            child.pid_fd = detail::open_pidfd(pid);
            return child;
        } else {
            auto next_start = make_next_start<PropagateDone>(done_fd);
            return start_stage_child<Stage::policy>(
                stage_def.so_path,
                input,
                stage_def.compute,
                stage_def.teardown,
                next_start,
                done_fd,
                close_fd);
        }
    }

    template <class Input>
    void run(int read_fd, Input input, int done_fd = -1, bool start_now = false) {
        constexpr bool emit_done = std::is_same_v<Next, std::nullptr_t>;
        const bool propagate_done = done_fd >= 0;
        if (propagate_done) {
            if (start_now)
                run_impl<emit_done, true, true>(read_fd, done_fd, input);
            else
                run_impl<emit_done, true, false>(read_fd, done_fd, input);
        } else {
            if (start_now)
                run_impl<false, false, true>(read_fd, -1, input);
            else
                run_impl<false, false, false>(read_fd, -1, input);
        }
    }

    void run(int read_fd, int done_fd = -1, bool start_now = false) {
        static_assert(std::is_same_v<typename Stage::input_type, void>,
                      "first stage requires input");
        run(read_fd, detail::NoInput{}, done_fd, start_now);
    }

    template <class Input>
    auto start(Input input, int close_fd = -1, int done_fd = -1) {
        constexpr bool emit_done = std::is_same_v<Next, std::nullptr_t>;
        const bool propagate_done = done_fd >= 0;
        if (propagate_done)
            return start_child_impl<emit_done, true>(input, done_fd, close_fd);
        return start_child_impl<false, false>(input, -1, close_fd);
    }
};

template <class Node>
struct all_always : std::false_type {};

template <>
struct all_always<std::nullptr_t> : std::true_type {};

template <class Stage, class Next>
struct all_always<Pipeline<Stage, Next>>
    : std::bool_constant<
          (Stage::policy == RunPolicy::Always || Stage::policy == RunPolicy::AlwaysReload) &&
          all_always<Next>::value> {};

static std::nullptr_t make_pipeline() { return nullptr; }

template <class Stage, class... Rest>
static auto make_pipeline(Stage stage_def, Rest... rest) {
    auto next = make_pipeline(rest...);
    using PipelineT = Pipeline<Stage, decltype(next)>;
    static_assert(
        (Stage::policy != RunPolicy::Always && Stage::policy != RunPolicy::AlwaysReload) ||
        all_always<decltype(next)>::value,
        "RunPolicy::Always/AlwaysReload requires all downstream stages to be RunPolicy::Always");
    return PipelineT{stage_def, next};
}
