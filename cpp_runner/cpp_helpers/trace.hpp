#pragma once

// ---------------------------------------------------------------------------
// Tracing / profiling infrastructure
//
// All code in this header is compiled away to nothing unless -DTRACE is set.
//
// Two stable, machine-parsable output line types:
//
//   PROFILE <section_name> <nanoseconds>
//   COUNT   <counter_name> <value>
//
// PROFILE_SCOPE accumulates time across all calls with the same name.
// Aggregated results are returned via trace_get_and_clear() after TRACE_FLUSH().
//
// Usage:
//   TRACE_RESET();                         // clear accumulators for a fresh run
//   PROFILE_SCOPE("q1_scan_total");        // times until end of enclosing scope
//   TRACE_COUNT("q1_rows_scanned", n);     // emit a COUNT line immediately
//   TRACE_ACCUM("buffer_pool_bytes", n);   // accumulate count until flush
//   TRACE_FLUSH();                         // flush accumulated profiles to buffer
//   // trace_get_and_clear() is called by the plugin host to retrieve & clear
// ---------------------------------------------------------------------------

#include <string>

// FILE_VERSION: 1

#ifdef TRACE

#include <cstdint>
#include <cstdio>
#include <ctime>
#include <mutex>
#include <unordered_map>

// ---------------------------------------------------------------------------
// Internal state
// ---------------------------------------------------------------------------
namespace trace_detail {

struct State {
    std::mutex                                mtx;
    std::string                               buffer;
    std::unordered_map<std::string, uint64_t> accumulator;
    std::unordered_map<std::string, long long> count_accumulator;
};

inline State& state() {
    static State s;
    return s;
}

// Caller must hold s.mtx.
inline void flush_profiles_locked(State& s) {
    char line[256];
    for (auto& [name, ns] : s.accumulator) {
        int n = std::snprintf(line, sizeof(line), "PROFILE %s %llu\n",
                              name.c_str(), static_cast<unsigned long long>(ns));
        if (n > 0 && n < (int)sizeof(line))
            s.buffer.append(line, static_cast<size_t>(n));
    }
    s.accumulator.clear();

    for (auto& [name, value] : s.count_accumulator) {
        int n = std::snprintf(line, sizeof(line), "COUNT %s %lld\n",
                              name.c_str(), value);
        if (n > 0 && n < (int)sizeof(line))
            s.buffer.append(line, static_cast<size_t>(n));
    }
    s.count_accumulator.clear();
}

} // namespace trace_detail

// ---------------------------------------------------------------------------
// Public helpers
// ---------------------------------------------------------------------------
inline void trace_reset() {
    auto& s = trace_detail::state();
    std::lock_guard<std::mutex> lk(s.mtx);
    s.accumulator.clear();
    s.count_accumulator.clear();
    s.buffer.clear();
}

inline void trace_flush() {
    auto& s = trace_detail::state();
    std::lock_guard<std::mutex> lk(s.mtx);
    trace_detail::flush_profiles_locked(s);
}

inline void trace_count(const char* name, long long value) {
    auto& s = trace_detail::state();
    std::lock_guard<std::mutex> lk(s.mtx);
    char line[256];
    int n = std::snprintf(line, sizeof(line), "COUNT %s %lld\n", name, value);
    if (n > 0 && n < (int)sizeof(line))
        s.buffer.append(line, static_cast<size_t>(n));
}

inline void trace_accum_count(const char* name, long long value) {
    auto& s = trace_detail::state();
    std::lock_guard<std::mutex> lk(s.mtx);
    s.count_accumulator[name] += value;
}

// Flush accumulated PROFILE entries into the buffer, return the full buffer
// contents, and clear the buffer. Called once per query run by the plugin host.
inline std::string trace_get_and_clear() {
    auto& s = trace_detail::state();
    std::lock_guard<std::mutex> lk(s.mtx);
    trace_detail::flush_profiles_locked(s);
    std::string result = std::move(s.buffer);
    s.buffer.clear();
    return result;
}

// ---------------------------------------------------------------------------
// High-resolution wall-clock in nanoseconds
// ---------------------------------------------------------------------------
inline uint64_t trace_now_ns() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<uint64_t>(ts.tv_sec) * 1'000'000'000ULL
         + static_cast<uint64_t>(ts.tv_nsec);
}

// ---------------------------------------------------------------------------
// RAII scoped timer - accumulates elapsed time into the named bucket
// ---------------------------------------------------------------------------
class ScopedTimer {
    const char* name_;
    uint64_t    start_ns_;
public:
    explicit ScopedTimer(const char* n)
        : name_(n), start_ns_(trace_now_ns()) {}

    ~ScopedTimer() {
        // Measure elapsed before acquiring the lock to keep lock hold time minimal.
        uint64_t elapsed = trace_now_ns() - start_ns_;
        auto& s = trace_detail::state();
        std::lock_guard<std::mutex> lk(s.mtx);
        s.accumulator[name_] += elapsed;
    }

    ScopedTimer(const ScopedTimer&)            = delete;
    ScopedTimer& operator=(const ScopedTimer&) = delete;
};

// ---------------------------------------------------------------------------
// Macros
// ---------------------------------------------------------------------------

// Clear accumulators and buffer - call once at start of run.
#define TRACE_RESET() trace_reset()

// Flush all accumulated PROFILE entries to the in-memory buffer.
#define TRACE_FLUSH() trace_flush()

// Place at the top of a scope - accumulates time into the named bucket.
// Uses __LINE__ (standard C++) so multiple invocations in the same scope compile.
#define PROFILE_SCOPE_IMPL(name, ctr) ScopedTimer _scoped_timer_##ctr(name)
#define PROFILE_SCOPE(name) PROFILE_SCOPE_IMPL(name, __LINE__)

// Emit a COUNT line immediately.
#define TRACE_COUNT(name, value) trace_count((name), static_cast<long long>(value))

// Accumulate a count and emit one COUNT line on TRACE_FLUSH()/trace_get_and_clear().
#define TRACE_ACCUM(name, value) trace_accum_count((name), static_cast<long long>(value))

#else // !TRACE  ---------------------------------------------------------------

// Zero-overhead stubs
#define TRACE_RESET()              do {} while (0)
#define TRACE_FLUSH()              do {} while (0)
#define PROFILE_SCOPE(name)        do {} while (0)
#define TRACE_COUNT(name, value)   do {} while (0)
#define TRACE_ACCUM(name, value)   do {} while (0)

inline std::string trace_get_and_clear() { return ""; }

#endif // TRACE
