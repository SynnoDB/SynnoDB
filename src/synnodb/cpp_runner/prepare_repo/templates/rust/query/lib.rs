//! The query crate: dispatch a batch of requests to the per-query files.
//!
//! Read-only scaffold (the Rust counterpart of query_impl.cpp). The module list
//! and the dispatch arms are generated from the run's query set; you do not edit
//! this file -- put query logic in `q<N>.rs`.
//!
//! The C ABI lives in the `plugins/query` crate, not here: plugin_query is
//! #[no_mangle] and must be linked into exactly one .so.
//!
//! FILE_VERSION: 1

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::time::Instant;

use engine_builder::Database;
use synno_rt::abi::panic_message;
use synno_rt::result_writer::write_result;

pub mod args;
// <<query_modules>>

/// One query's outcome, in Rust terms. The shim turns it into the C struct.
pub struct QueryOutcome {
    pub query_id: String,
    pub req_id: String,
    pub trace: String,
    pub error: String,
    pub elapsed_ms: f64,
}

/// One parsed request off the wire: `<query_id> <req_id> <args...>`.
struct QueryRequest {
    query_id: String,
    req_id: String,
    line: String,
}

fn parse_request(line: &str) -> Option<QueryRequest> {
    let mut it = line.splitn(3, char::is_whitespace);
    let query_id = it.next()?.to_string();
    let req_id = it.next()?.to_string();
    if query_id.is_empty() || req_id.is_empty() {
        return None;
    }
    Some(QueryRequest {
        query_id,
        req_id,
        line: it.next().unwrap_or("").to_string(),
    })
}

/// Run one request: dispatch, time it, and write its Arrow result where Python
/// reads it ($SYNNODB_RESULT_DIR/result_<req_id>.arrow).
///
/// A panic inside a query file is caught here, so one bad query reports an error
/// instead of taking the engine process down mid-batch.
fn run_one(db: &Database, req: &QueryRequest) -> (f64, String) {
    let prefix = format!("Q{}({}): ", req.query_id, req.req_id);

    let started = Instant::now();
    let outcome = catch_unwind(AssertUnwindSafe(|| -> synno_rt::Result<()> {
        let table = match req.query_id.as_str() {
            // <<impl_fn_calls>>
            other => Err(synno_rt::Error::new(format!("unknown query id: {other}"))),
        }?;
        write_result(&table, &req.req_id)
    }));
    let elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;

    match outcome {
        Ok(Ok(())) => (elapsed_ms, String::new()),
        Ok(Err(e)) => (elapsed_ms, format!("{prefix}{e}")),
        Err(p) => (
            elapsed_ms,
            format!("{prefix}panicked: {}", panic_message(&p)),
        ),
    }
}

/// Run a whole RUN batch. Called by the plugin shim.
pub fn run_batch(db: &Database, lines: &[String]) -> Vec<QueryOutcome> {
    let mut out = Vec::new();
    for line in lines {
        let Some(req) = parse_request(line) else {
            continue;
        };
        synno_rt::trace::reset();
        let (elapsed_ms, error) = run_one(db, &req);
        out.push(QueryOutcome {
            query_id: req.query_id,
            req_id: req.req_id,
            trace: synno_rt::trace::flush(),
            error,
            elapsed_ms,
        });
    }
    out
}
