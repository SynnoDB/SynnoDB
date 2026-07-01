// db_launch: a tiny exec-in-place launcher for the SynnoDB engine ("./db").
//
// HotpatchProc launches the engine as its *direct* child and depends on that child
// being the real ./db: it passes custom control-pipe fds (P2C_FD/C2P_FD) and tracks
// the child PID for wait()/exit-classification and signalling. To impose a cgroup
// memory ceiling and robust parent-death semantics without breaking those
// assumptions, this launcher performs the per-process setup and then execs the
// target *in place*, so the kernel keeps the same PID and all inherited
// (non-CLOEXEC) file descriptors. Because it execs in place, Popen still sees the
// final ./db as the same child it forked.
//
// It is deliberately tiny and boring: a fixed sequence of syscalls then execvp, no
// dynamic allocation and no parsing beyond its own flags, so it can never become a
// failure source of its own.
//
// Usage:
//   db_launch [--cgroup <dir>] [--as-limit <bytes>] -- <prog> [args...]
//
// Steps, in order:
//   1. capture the original parent pid;
//   2. prctl(PR_SET_PDEATHSIG, SIGKILL) so the kernel kills us when the parent dies
//      (survives execve, so it protects the real ./db too), verified - fail closed;
//   3. re-read getppid(): if it changed from the captured value the parent already
//      died in the fork/exec window, so exit rather than run orphaned;
//   4. setsid() so the engine tree is its own session (a last-resort killpg target);
//   5. if --cgroup is given, join it by writing our pid to <dir>/cgroup.procs. Fail
//      closed: if the join fails we do NOT exec, so an unbounded engine never starts;
//   6. if --as-limit is given, setrlimit(RLIMIT_AS, ...) as a cheap VA fast-fail;
//   7. execvp the target. Any failure before exec writes a short diagnostic and
//      exits non-zero with a distinct code.

#include <cerrno>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include <fcntl.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <unistd.h>

namespace {

// Distinct exit codes so a launch failure is diagnosable from the child's status.
constexpr int kUsage = 64;        // bad invocation
constexpr int kPrctlFailed = 81;  // could not arm PR_SET_PDEATHSIG
constexpr int kParentGone = 82;   // parent died in the fork/exec window
constexpr int kCgroupFailed = 83; // could not join the requested cgroup
constexpr int kRlimitFailed = 84; // could not set RLIMIT_AS
constexpr int kSetsidFailed = 85; // could not start a new session
constexpr int kExecFailed = 127;  // execvp of the target failed

void die(const char* what, int code) {
    int e = errno;
    if (e != 0) {
        dprintf(STDERR_FILENO, "db_launch: %s: %s\n", what, std::strerror(e));
    } else {
        dprintf(STDERR_FILENO, "db_launch: %s\n", what);
    }
    _exit(code);
}

// Parse an unsigned byte count; exits on malformed input rather than guessing.
unsigned long long parse_bytes(const char* s) {
    errno = 0;
    char* end = nullptr;
    unsigned long long v = std::strtoull(s, &end, 10);
    if (errno != 0 || end == s || (end && *end != '\0')) {
        die("invalid byte count for --as-limit", kUsage);
    }
    return v;
}

// Join a cgroup v2 cgroup by writing our pid to <dir>/cgroup.procs.
void join_cgroup(const char* dir) {
    char procs[PATH_MAX];
    int n = std::snprintf(procs, sizeof procs, "%s/cgroup.procs", dir);
    if (n <= 0 || static_cast<size_t>(n) >= sizeof procs) {
        errno = ENAMETOOLONG;
        die("cgroup path too long", kCgroupFailed);
    }
    int fd = ::open(procs, O_WRONLY | O_CLOEXEC);
    if (fd < 0) {
        die("open cgroup.procs", kCgroupFailed);
    }
    char pidbuf[32];
    int pn = std::snprintf(pidbuf, sizeof pidbuf, "%ld", static_cast<long>(::getpid()));
    // cgroup.procs accepts the pid in a single write; a short write is a real error.
    if (pn <= 0 || ::write(fd, pidbuf, static_cast<size_t>(pn)) != pn) {
        int saved = errno;
        ::close(fd);
        errno = saved;
        die("write pid to cgroup.procs", kCgroupFailed);
    }
    ::close(fd);
}

}  // namespace

int main(int argc, char** argv) {
    const char* cgroup_dir = nullptr;
    const char* as_limit = nullptr;

    // Parse our own flags up to the "--" separator; everything after it is the
    // target program and its arguments, passed through untouched.
    int i = 1;
    for (; i < argc; ++i) {
        if (std::strcmp(argv[i], "--") == 0) {
            ++i;
            break;
        }
        if (std::strcmp(argv[i], "--cgroup") == 0) {
            if (++i >= argc) die("--cgroup requires a directory argument", kUsage);
            cgroup_dir = argv[i];
        } else if (std::strcmp(argv[i], "--as-limit") == 0) {
            if (++i >= argc) die("--as-limit requires a byte-count argument", kUsage);
            as_limit = argv[i];
        } else {
            dprintf(STDERR_FILENO, "db_launch: unknown flag '%s'\n", argv[i]);
            _exit(kUsage);
        }
    }
    if (i >= argc) {
        die("no target program after '--'", kUsage);
    }
    char** target_argv = &argv[i];

    // 1-3. Parent-death setup, race-checked against the captured original parent.
    pid_t ppid_before = ::getppid();
    if (::prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0) != 0) {
        die("prctl(PR_SET_PDEATHSIG)", kPrctlFailed);
    }
    // If the parent died between getppid() and prctl(), the death signal we just
    // armed references a parent that is already gone; bail rather than run orphaned.
    if (::getppid() != ppid_before) {
        die("parent exited before launch completed", kParentGone);
    }

    // 4. Own session, so the engine tree can be reaped as a group as a last resort.
    //    A freshly forked child is never a group leader, so setsid() must succeed;
    //    treat failure as fatal rather than launching outside its own session.
    if (::setsid() < 0) {
        die("setsid", kSetsidFailed);
    }

    // 5. Join the memory-capped cgroup before exec (fail closed). Keep everything from the parent
    //    creating this cgroup up to this join non-blocking: cgroup.py's stale-cgroup sweep treats a
    //    runner cgroup with an empty cgroup.procs older than 60s as abandoned, so a multi-second
    //    stall between the cgroup's creation and this pid-join could let a concurrent orchestrator's
    //    sweep race it. The steps above (prctl/getppid/setsid) are trivial by design; keep them so.
    if (cgroup_dir != nullptr) {
        join_cgroup(cgroup_dir);
    }

    // 6. Virtual-memory fast-fail (RLIMIT_AS). Cheap guard; the cgroup is the real ceiling.
    if (as_limit != nullptr) {
        unsigned long long bytes = parse_bytes(as_limit);
        struct rlimit rl;
        rl.rlim_cur = static_cast<rlim_t>(bytes);
        rl.rlim_max = static_cast<rlim_t>(bytes);
        if (::setrlimit(RLIMIT_AS, &rl) != 0) {
            die("setrlimit(RLIMIT_AS)", kRlimitFailed);
        }
    }

    // 7. Become the target, in place: same PID, inherited fds preserved.
    ::execvp(target_argv[0], target_argv);
    die("execvp target", kExecFailed);  // only reached if execvp failed
}
