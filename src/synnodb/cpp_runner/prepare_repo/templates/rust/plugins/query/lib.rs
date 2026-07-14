//! libquery.so - the C ABI shim for the query plugin.
//!
//! Read-only scaffold. The Rust counterpart of api/query_api.cpp: it marshals the
//! batch across the boundary as plain C and makes sure nothing unwinds across it.

use std::ffi::{c_char, c_void};
use std::panic::{catch_unwind, AssertUnwindSafe};

use engine_builder::Database;
use engine_query::run_batch;
use synno_rt::abi::{
    free_c_string, panic_message, query_lines, to_c_string, SynnoQueryApi, SynnoQueryBatchResult,
    SynnoQueryResult,
};

extern "C" fn abi_query(
    db: *mut c_void,
    lines: *const *const c_char,
    n_lines: usize,
    out: *mut SynnoQueryBatchResult,
) {
    if out.is_null() {
        return;
    }

    // A panic must not unwind across the C ABI (it is undefined). Catch it and
    // report the batch as failed -- exactly what the C++ shim does with an
    // exception, so the host's stage-error path is unchanged.
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        let db = unsafe { &*(db as *const Database) };
        let lines = unsafe { query_lines(lines, n_lines) };
        run_batch(db, &lines)
    }));

    let (rows, stage_error) = match outcome {
        Ok(rows) => (rows, String::new()),
        Err(p) => (
            Vec::new(),
            format!("query stage panicked: {}", panic_message(&p)),
        ),
    };

    let results: Vec<SynnoQueryResult> = rows
        .into_iter()
        .map(|r| SynnoQueryResult {
            query_id: to_c_string(&r.query_id),
            req_id: to_c_string(&r.req_id),
            trace: to_c_string(&r.trace),
            error: to_c_string(&r.error),
            elapsed_ms: r.elapsed_ms,
        })
        .collect();

    let mut boxed = results.into_boxed_slice();
    let len = boxed.len();
    let ptr = boxed.as_mut_ptr();
    std::mem::forget(boxed);

    unsafe {
        (*out).results = ptr;
        (*out).len = len;
        (*out).stage_error = to_c_string(&stage_error);
    }
}

extern "C" fn abi_free_results(out: *mut SynnoQueryBatchResult) {
    if out.is_null() {
        return;
    }
    unsafe {
        if !(*out).results.is_null() && (*out).len > 0 {
            let slice = std::slice::from_raw_parts_mut((*out).results, (*out).len);
            for r in slice.iter() {
                free_c_string(r.query_id);
                free_c_string(r.req_id);
                free_c_string(r.trace);
                free_c_string(r.error);
            }
            drop(Box::from_raw(slice as *mut [SynnoQueryResult]));
        }
        free_c_string((*out).stage_error);
        (*out).results = std::ptr::null_mut();
        (*out).len = 0;
        (*out).stage_error = std::ptr::null();
    }
}

static QUERY: SynnoQueryApi = SynnoQueryApi {
    query: abi_query,
    free_results: abi_free_results,
};

#[no_mangle]
pub extern "C" fn plugin_query() -> *const c_void {
    &QUERY as *const SynnoQueryApi as *const c_void
}
