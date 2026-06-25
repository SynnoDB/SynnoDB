#pragma once

// ──────────────────────────────────────────────────────────────────────────────
// ThreadPool: parallel-for thread pool with CPU affinity support.
//
// Design
//   • Workers use a two-phase idle strategy:
//       1. Spin for idle_spin_limit iterations after each task (fast re-dispatch).
//       2. Fall back to condition_variable::wait when idle_spin_limit is reached.
//     This keeps dispatch latency low for back-to-back queries while freeing the
//     CPU when no work is pending.
//   • Workers are pinned to core_ids[tid] via pin_process_to_cpu on startup.
//   • spin_yield_after: during the spin phase, after this many _mm_pause spins
//     workers call yield(). 0 = pure spin within the spin phase.
//   • idle_spin_limit: total _mm_pause iterations before sleeping on condvar.
//     Default ~100k ≈ a few hundred μs. Set to 0 for pure spin (dedicated cores).
//   • parallel_for uses fn+ctx type-erasure — zero heap allocation per call.
//   • Not re-entrant: do not call parallel_for from within a worker task.
//   • x86 only (_mm_pause).
//
// Quick start
//   ThreadPool pool(4);                   // 4 threads, sleep-when-idle
//   pool.parallel_for([&](int tid, int n) {
//       // tid in [0, n), all threads run concurrently
//   });
// ──────────────────────────────────────────────────────────────────────────────

#include <atomic>
#include <condition_variable>
#include <cstdlib>
#include <immintrin.h>
#include <mutex>
#include <thread>
#include <vector>

#include "cpu_affinity.hpp"
// cpu_affinity.hpp provides pin_process_to_cpu(int core_id).

struct ThreadPool {
    // ── Public config (set before init, or via constructor) ────────────────
    int              num_threads      = 1;
    int              spin_yield_after = 0;       // 0 = pure spin within spin phase
    int              idle_spin_limit  = 100'000; // spins before condvar sleep; 0 = pure spin
    std::vector<int> core_ids;

    // ── Internal ───────────────────────────────────────────────────────────
    std::vector<std::thread> workers;

    // Dispatch slot: fn+ctx written before generation bump (release),
    // read after workers observe the new generation (acquire). No data race.
    alignas(64) void (*task_fn)(void* ctx, int tid, int n_threads) = nullptr;
                void*  task_ctx = nullptr;

    alignas(64) std::atomic<int>  generation{0};
    alignas(64) std::atomic<int>  done_count{0};
    alignas(64) std::atomic<bool> shutdown{false};

    std::mutex              idle_mutex;
    std::condition_variable idle_cv;

    // ── Constructors ───────────────────────────────────────────────────────

    ThreadPool() = default;
    ThreadPool(const ThreadPool&)            = delete;
    ThreadPool& operator=(const ThreadPool&) = delete;

    // Construct and start immediately.
    // spin_yield: pause iterations before yield() within spin phase; 0 = pure spin.
    explicit ThreadPool(int n, std::vector<int> cores = {}, int spin_yield = 0)
    {
        spin_yield_after = spin_yield;
        init(n, std::move(cores));
    }

    ~ThreadPool() {
        shutdown.store(true, std::memory_order_relaxed);
        generation.fetch_add(1, std::memory_order_release);
        idle_cv.notify_all();
        for (auto& t : workers) t.join();
    }

    // ── Init (deferred form for default-constructed pools) ─────────────────
    //
    // Each worker thread is pinned to core_ids[tid] via pin_process_to_cpu.
    // If core_ids is empty or too short the corresponding worker is unpinned.
    void init(int n, std::vector<int> cores = {})
    {
        num_threads = n;
        core_ids    = std::move(cores);

        const int last_gen = generation.load(std::memory_order_relaxed);
        workers.reserve((size_t)(num_threads - 1));
        for (int tid = 1; tid < num_threads; ++tid) {
            workers.emplace_back([this, tid, last_gen]() mutable noexcept {
                if ((int)core_ids.size() > tid)
                    try { pin_process_to_cpu(core_ids[tid]); } catch (...) {}

                int my_gen = last_gen;
                while (true) {
                    // Phase 1: spin briefly for fast re-dispatch after a task.
                    int spins = 0;
                    bool slept = false;
                    while (generation.load(std::memory_order_acquire) == my_gen) {
                        _mm_pause();
                        if (spin_yield_after > 0 && (spins % spin_yield_after) == (spin_yield_after - 1))
                            std::this_thread::yield();
                        if (idle_spin_limit > 0 && ++spins >= idle_spin_limit) {
                            // Phase 2: sleep until parallel_for wakes us.
                            std::unique_lock<std::mutex> lk(idle_mutex);
                            idle_cv.wait(lk, [&]{
                                return generation.load(std::memory_order_relaxed) != my_gen;
                            });
                            slept = true;
                            break;
                        }
                    }
                    (void)slept;
                    my_gen = generation.load(std::memory_order_acquire);
                    if (shutdown.load(std::memory_order_relaxed)) break;
                    task_fn(task_ctx, tid, num_threads);
                    done_count.fetch_add(1, std::memory_order_release);
                }
            });
        }
    }

    // ── parallel_for ───────────────────────────────────────────────────────
    //
    // Calls f(tid, n_threads) on all n_threads threads concurrently, then
    // blocks until all workers finish.  The callable f must outlive this call
    // (it is captured by pointer, not copied).
    template <typename F>
    void parallel_for(F&& f) {
        if (num_threads <= 1) { f(0, 1); return; }

        // Type-erase F into a plain fn pointer + void* context.
        // No heap allocation: f lives on the caller's stack for this call.
        using FT    = std::remove_reference_t<F>;
        task_fn     = [](void* ctx, int tid, int n) {
            (*static_cast<FT*>(ctx))(tid, n);
        };
        task_ctx = static_cast<void*>(&f);

        done_count.store(0, std::memory_order_relaxed);
        generation.fetch_add(1, std::memory_order_release); // wakes spinning workers
        idle_cv.notify_all();                                // wakes sleeping workers

        f(0, num_threads); // main thread runs as tid 0

        // Wait for all workers.
        const int need = num_threads - 1;
        int spins = 0;
        while (done_count.load(std::memory_order_acquire) < need) {
            _mm_pause();
            if (spin_yield_after > 0 && ++spins >= spin_yield_after) {
                std::this_thread::yield();
                spins = 0;
            }
        }
    }
};

// ── init_thread_pool ──────────────────────────────────────────────────────────
//
// Convenience helper: reads CORE_IDS env var, then calls pool.init().
// Thread count and CPU pinning are both derived from the list.
//
// CORE_IDS=0,2,4,6 ./my_program  — 4 threads pinned to cores 0,2,4,6
//
// If CORE_IDS is unset, defaults to all hardware threads pinned to 0..n-1.
inline void init_thread_pool(ThreadPool& pool, int spin_yield_after = 0)
{
    std::vector<int> cores;
    const char* env_c = std::getenv("CORE_IDS");
    if (env_c && *env_c) {
        const char* p = env_c;
        while (*p) {
            while (*p == ',' || *p == ' ') ++p;
            if (!*p) break;
            char* end;
            int id = (int)std::strtol(p, &end, 10);
            if (end == p) break;
            cores.push_back(id);
            p = end;
        }
    }

    if (cores.empty()) {
        int n = (int)std::thread::hardware_concurrency();
        if (n <= 0) n = 1;
        cores.resize((size_t)n);
        for (int i = 0; i < n; ++i) cores[i] = i;
    }

    pool.spin_yield_after = spin_yield_after;
    int n = (int)cores.size();
    pool.init(n, std::move(cores));
}


// implemented in query_impl.cpp, shared thread pool for all query functions
ThreadPool& get_query_pool();
