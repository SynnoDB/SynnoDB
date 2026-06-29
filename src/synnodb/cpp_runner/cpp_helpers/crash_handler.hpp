#pragma once
// crash_handler.hpp - turn an engine SIGSEGV/SIGABRT/... into an actionable,
// LLM-readable stack trace on stderr instead of a bare "killed by signal 11".
//
// A fatal memory fault (out-of-bounds read, null deref, stack overflow) inside a
// generated run_qN bypasses the per-query try/catch in query_impl.cpp and kills the
// engine child. Without this the only feedback is the parent's "query child killed by
// signal 11 (Segmentation fault)" - no function, no file, no line - which is almost
// impossible to debug. query_impl.cpp installs this handler once and tags the running
// query via set_query_context(); on a fatal async signal the handler prints the signal
// name, the faulting address, the current query, and a symbolized backtrace to fd 2.
// That fd is captured by the Python runner (hotpatch_proc.py) and surfaced to the model,
// so a crash reads as "FATAL SIGSEGV ... run_q10 ... query10.cpp:289".
//
// The handler stays as close to async-signal-safe as a symbolizing backtrace allows: a
// fixed char buffer (no malloc, no std::string) and direct write(2). backtrace() is warmed
// up at install time so the unwinder's one-time libgcc load does not happen inside the
// handler. Its one best-effort call is dladdr() (return address -> module + load-relative
// offset, for offline addr2line); dladdr takes the dynamic-linker lock, so it is fully safe
// only when the fault did not occur while that lock was held - which is the case for the
// faults this exists to catch (out-of-bounds reads in generated run_qN code, not faults
// inside dlopen). We deliberately avoid backtrace_symbols_fd, which re-runs the same
// resolution for no extra information.

#include <atomic>
#include <csignal>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <dlfcn.h>
#include <execinfo.h>
#include <initializer_list>
#include <unistd.h>

namespace synnodb {
namespace crash {

// Fixed-size, lock-free context buffer naming the running query. Written from the query
// thread before each dispatch; read from the signal handler. Intentionally a raw buffer
// so the handler never allocates or locks.
inline char* context_buffer() {
    static char buf[256] = {0};
    return buf;
}

// Bounded copy of the running-query description, e.g. "run #3 Q10(<inst>)". Safe to call
// from normal code; the handler only ever reads the buffer.
inline void set_query_context(const char* ctx) {
    char* buf = context_buffer();
    if (!ctx) {
        buf[0] = '\0';
        return;
    }
    std::size_t i = 0;
    for (; i + 1 < 256 && ctx[i] != '\0'; ++i) buf[i] = ctx[i];
    buf[i] = '\0';
}

namespace detail {

// async-signal-safe write of a NUL-terminated string to stderr.
inline void safe_write(const char* s) {
    if (!s) return;
    std::size_t n = 0;
    while (s[n] != '\0') ++n;
    ssize_t w = ::write(STDERR_FILENO, s, n);
    (void)w;
}

// async-signal-safe hex print of a pointer value (for the faulting address).
inline void safe_write_hex(unsigned long long v) {
    char tmp[2 + 16 + 1];
    tmp[0] = '0';
    tmp[1] = 'x';
    int pos = 2;
    if (v == 0) {
        tmp[pos++] = '0';
    } else {
        char digits[16];
        int d = 0;
        while (v != 0 && d < 16) {
            const int nib = static_cast<int>(v & 0xF);
            digits[d++] = static_cast<char>(nib < 10 ? '0' + nib : 'a' + (nib - 10));
            v >>= 4;
        }
        while (d > 0) tmp[pos++] = digits[--d];
    }
    tmp[pos] = '\0';
    safe_write(tmp);
}

inline const char* signal_name(int sig) {
    switch (sig) {
        case SIGSEGV: return "SIGSEGV (segmentation fault, 11)";
        case SIGABRT: return "SIGABRT (abort, 6)";
        case SIGBUS:  return "SIGBUS (bus error, 7)";
        case SIGFPE:  return "SIGFPE (arithmetic error, 8)";
        case SIGILL:  return "SIGILL (illegal instruction, 4)";
        default:      return "fatal signal";
    }
}

inline void handler(int sig, siginfo_t* info, void* /*ucontext*/) {
    safe_write("\n=================== SynnoDB engine CRASH ===================\n");
    safe_write("FATAL SIGNAL: ");
    safe_write(signal_name(sig));
    safe_write("\n");
    if (info != nullptr && (sig == SIGSEGV || sig == SIGBUS)) {
        safe_write("fault address: ");
        safe_write_hex(reinterpret_cast<unsigned long long>(info->si_addr));
        safe_write("\n");
    }
    safe_write("while executing: ");
    const char* ctx = context_buffer();
    safe_write(ctx[0] != '\0' ? ctx : "(no query context set)");
    safe_write("\nC++ backtrace (most recent call first):\n");

    void* frames[64];
    const int n = ::backtrace(frames, 64);
    // Print one parseable line per frame: "<module>(+0x<file_offset>) <mangled_symbol>".
    // The module + file-relative offset is what addr2line consumes to recover file:line
    // (query_validator_class.py does that); the mangled symbol (e.g. _Z7run_q10...) is
    // demangled there for readability. dladdr is used instead of backtrace_symbols so we
    // can emit the load-base-relative offset directly; when it cannot identify the module we
    // emit the raw address rather than calling backtrace_symbols_fd (same resolution, but it
    // would take the dynamic-linker lock a second time).
    for (int i = 0; i < n; ++i) {
        Dl_info dli;
        if (::dladdr(frames[i], &dli) != 0 && dli.dli_fname != nullptr) {
            const unsigned long long off =
                reinterpret_cast<uintptr_t>(frames[i]) -
                reinterpret_cast<uintptr_t>(dli.dli_fbase);
            safe_write("  ");
            safe_write(dli.dli_fname);
            safe_write("(+");
            safe_write_hex(off);
            safe_write(") ");
            safe_write(dli.dli_sname != nullptr ? dli.dli_sname : "??");
            safe_write("\n");
        } else {
            // dladdr could not identify the module: emit the raw runtime address (fully
            // async-signal-safe). No backtrace_symbols_fd fallback - it would just re-run the
            // dladdr resolution that already failed.
            safe_write("  ");
            safe_write_hex(reinterpret_cast<unsigned long long>(frames[i]));
            safe_write("\n");
        }
    }
    safe_write("============================================================\n");

    // SA_RESETHAND already restored the default disposition; re-raise so the parent
    // still observes the original signal (and a core dump is produced if enabled).
    ::raise(sig);
}

}  // namespace detail

// Install fatal-signal handlers once per process. Idempotent and thread-safe. Uses an
// alternate signal stack so a stack-overflow SIGSEGV can still be reported.
inline void install_crash_handler() {
    static std::atomic<bool> installed{false};
    bool expected = false;
    if (!installed.compare_exchange_strong(expected, true)) return;

    // Warm up the unwinder so the first (one-time) libgcc load does not run inside the
    // handler, where allocation would be unsafe.
    void* warm[4];
    (void)::backtrace(warm, 4);

    static const std::size_t kAltStackSize = 1 << 16;  // 64 KiB, ample for backtrace
    static char altstack[kAltStackSize];
    stack_t ss;
    ss.ss_sp = altstack;
    ss.ss_size = kAltStackSize;
    ss.ss_flags = 0;
    ::sigaltstack(&ss, nullptr);

    struct sigaction sa;
    std::memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = detail::handler;
    sa.sa_flags = SA_SIGINFO | SA_ONSTACK | SA_RESETHAND;
    ::sigemptyset(&sa.sa_mask);
    for (int sig : {SIGSEGV, SIGABRT, SIGBUS, SIGFPE, SIGILL}) {
        ::sigaction(sig, &sa, nullptr);
    }
}

}  // namespace crash

// Convenience aliases at the synnodb scope (mirrors the flat helper API used elsewhere).
inline void install_crash_handler() {
#if defined(__SANITIZE_ADDRESS__)
    // Under AddressSanitizer, ASan's own redzone/signal reporting is strictly more
    // precise (it names the variable and the exact source line). Installing our handler
    // would override ASan's SIGSEGV handler, so defer to ASan entirely.
    return;
#else
    crash::install_crash_handler();
#endif
}
inline void set_query_context(const char* ctx) { crash::set_query_context(ctx); }

}  // namespace synnodb
