//! The query pool: parallel_for / parallel_reduce over rayon.
//!
//! The Rust counterpart of `thread_pool.hpp` + `query_pool.hpp`. The pool itself
//! is NOT ported -- rayon already is a work-stealing pool -- but two contracts
//! from the C++ side are, because the framework depends on them:
//!
//! 1. `CORE_IDS` sizes and pins the pool. It is a comma-separated list of core
//!    ids: the THREAD COUNT IS THE LIST LENGTH, and each worker is pinned to one
//!    of them. Base validation runs with `CORE_IDS=1`, which means one thread on
//!    core 1 -- not "core ids up to 1". Reading it as anything else would run a
//!    supposedly single-threaded validation across every core and hide races.
//!
//! 2. At one thread these primitives take a serial fast path, so the base
//!    implementation is byte-identical to a plain loop and can be scaled later
//!    by raising the thread count alone -- no separate ST/MT code path.

use std::sync::OnceLock;

use rayon::prelude::*;
use rayon::ThreadPool;

fn core_ids() -> Vec<usize> {
    let raw = std::env::var("CORE_IDS").unwrap_or_default();
    let cores: Vec<usize> = raw
        .split(|c| c == ',' || c == ' ')
        .filter(|s| !s.is_empty())
        .filter_map(|s| s.parse::<usize>().ok())
        .collect();
    if !cores.is_empty() {
        return cores;
    }
    // Unset -> all hardware threads, pinned 0..n-1, matching init_thread_pool().
    let n = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1);
    (0..n).collect()
}

#[cfg(target_os = "linux")]
fn pin_to_core(core: usize) {
    // Pin this worker to one core, as the C++ pool does: the engine is benchmarked
    // under a fixed affinity, and an unpinned worker migrating across sockets
    // makes runtimes non-reproducible.
    unsafe {
        let mut set: libc::cpu_set_t = std::mem::zeroed();
        libc::CPU_ZERO(&mut set);
        libc::CPU_SET(core, &mut set);
        libc::sched_setaffinity(0, std::mem::size_of::<libc::cpu_set_t>(), &set);
    }
}

#[cfg(not(target_os = "linux"))]
fn pin_to_core(_core: usize) {}

static POOL: OnceLock<ThreadPool> = OnceLock::new();

/// The shared query pool, sized and pinned from `CORE_IDS`.
pub fn get_query_pool() -> &'static ThreadPool {
    POOL.get_or_init(|| {
        let cores = core_ids();
        let n = cores.len();
        rayon::ThreadPoolBuilder::new()
            .num_threads(n)
            .start_handler(move |idx| pin_to_core(cores[idx % cores.len()]))
            .build()
            .expect("synno_rt: failed to build the query pool")
    })
}

/// How many threads the engine runs queries at.
pub fn num_threads() -> usize {
    get_query_pool().current_num_threads()
}

/// Run `f(i)` for every i in `0..n`.
///
/// Serial fast path at one thread (see the module note): identical to a plain
/// loop, so a base implementation written with this is correct before it is ever
/// parallel.
pub fn parallel_for<F>(n: usize, f: F)
where
    F: Fn(usize) + Send + Sync,
{
    if num_threads() <= 1 {
        for i in 0..n {
            f(i);
        }
        return;
    }
    get_query_pool().install(|| (0..n).into_par_iter().for_each(f));
}

/// Fold `0..n` into private per-slice state, then merge the partials.
///
/// `fold(acc, i)` accumulates one element into a thread-private accumulator;
/// `combine(a, b)` merges two partials. `combine` must be associative and
/// commutative, and for DECIMAL/INT aggregates it must be exact (integer or
/// fixed-point) -- reducing an exact aggregate through floating point is what
/// the correctness gate catches.
pub fn parallel_reduce<T, F, C>(n: usize, identity: T, fold: F, combine: C) -> T
where
    T: Clone + Send + Sync,
    F: Fn(T, usize) -> T + Send + Sync,
    C: Fn(T, T) -> T + Send + Sync,
{
    if num_threads() <= 1 {
        let mut acc = identity;
        for i in 0..n {
            acc = fold(acc, i);
        }
        return acc;
    }
    get_query_pool().install(|| {
        (0..n)
            .into_par_iter()
            .fold(|| identity.clone(), &fold)
            .reduce(|| identity.clone(), &combine)
    })
}
