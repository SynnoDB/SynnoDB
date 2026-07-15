//! `synno_rt` - the runtime half of a generated Rust engine.
//!
//! An engine is three cdylib plugins (loader, builder, query) behind the C ABI
//! in `cpp_runner/api/plugin_abi.h`, loaded by the same C++ host that loads a
//! C++ engine. This crate is everything in those plugins that the model does NOT
//! write: reading Arrow columns in (`ingest`), building the exact Arrow result
//! out (`egress`), writing it where Python reads it (`result_writer`), the query
//! pool (`pool`), tracing (`trace`), and the ABI glue (`abi`).
//!
//! It is the Rust mirror of `cpp_helpers/` + `api/`, and the two must agree
//! value-for-value: `tests/test_column_ingest.py` and `test_column_egress.py`
//! run one table of cases against both. A divergence here does not fail a build,
//! it produces an engine that is quietly wrong on some queries.

pub mod abi;
pub mod args;
pub mod egress;
pub mod ingest;
pub mod pool;
pub mod result_writer;
pub mod shm;
pub mod trace;

use std::fmt;

/// An engine-side failure. Carried as data across the plugin ABI rather than
/// unwound, because a panic crossing an `extern "C"` frame is undefined.
#[derive(Debug, Clone)]
pub struct Error(String);

impl Error {
    pub fn new(msg: impl Into<String>) -> Self {
        Error(msg.into())
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Error {}

pub type Result<T> = std::result::Result<T, Error>;

/// What a generated query file works with.
pub mod prelude {
    pub use crate::args::ArgScanner;
    pub use crate::egress::{
        bool_column, date_column, decimal_column, double_column, hugeint_column, int64_column,
        integer_column, make_table, string_column, uint64_column, Validity,
    };
    pub use crate::ingest::{
        as_date_days, as_double, as_integer, as_string, scaled_integer, Nullable,
    };
    pub use crate::pool::{get_query_pool, num_threads, parallel_for, parallel_reduce};
    pub use crate::result_writer::write_result;
    pub use crate::{profile_scope, trace_count, Error, Result};
    pub use arrow::record_batch::RecordBatch;
}
