//! Scoped timings and counters, surfaced to the optimizer prompts.
//!
//! The Rust counterpart of `cpp_helpers/trace.hpp`. The trace string a query
//! returns is fed back to the model as the profile it optimizes against, so the
//! shape ("scope: N ms", "counter: N") is what the prompts teach the model to
//! read. Compiled away unless the `trace` feature is on, like `-DTRACE`.

use std::cell::RefCell;
use std::time::Instant;

thread_local! {
    static SCOPES: RefCell<Vec<(String, f64)>> = const { RefCell::new(Vec::new()) };
    static COUNTS: RefCell<Vec<(String, u64)>> = const { RefCell::new(Vec::new()) };
}

/// Times a region and records it on drop.
pub struct Scope {
    name: &'static str,
    start: Instant,
}

impl Scope {
    pub fn new(name: &'static str) -> Self {
        Self { name, start: Instant::now() }
    }
}

impl Drop for Scope {
    fn drop(&mut self) {
        let ms = self.start.elapsed().as_secs_f64() * 1000.0;
        SCOPES.with(|s| s.borrow_mut().push((self.name.to_string(), ms)));
    }
}

pub fn count(name: &str, n: u64) {
    COUNTS.with(|c| c.borrow_mut().push((name.to_string(), n)));
}

pub fn reset() {
    SCOPES.with(|s| s.borrow_mut().clear());
    COUNTS.with(|c| c.borrow_mut().clear());
}

/// Drain the trace for the query that just ran.
pub fn flush() -> String {
    let mut out = String::new();
    SCOPES.with(|s| {
        for (name, ms) in s.borrow_mut().drain(..) {
            out.push_str(&format!("{name}: {ms:.3} ms\n"));
        }
    });
    COUNTS.with(|c| {
        for (name, n) in c.borrow_mut().drain(..) {
            out.push_str(&format!("{name}: {n}\n"));
        }
    });
    out
}

/// `PROFILE_SCOPE`: time the enclosing block.
#[macro_export]
macro_rules! profile_scope {
    ($name:expr) => {
        let _synno_scope = if cfg!(feature = "trace") {
            Some($crate::trace::Scope::new($name))
        } else {
            None
        };
    };
}

/// `TRACE_COUNT`: record a row count at an operator boundary.
#[macro_export]
macro_rules! trace_count {
    ($name:expr, $n:expr) => {
        if cfg!(feature = "trace") {
            $crate::trace::count($name, $n as u64);
        }
    };
}
