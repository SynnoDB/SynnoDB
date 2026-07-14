//! Per-query argument structs and their parsers.
//!
//! Read-only scaffold, generated from the workload's placeholder specs (the Rust
//! counterpart of args_parser.hpp). The wire format is shared with the C++
//! engine; `synno_rt::args::ArgScanner` reads it.
//!
//! FILE_VERSION: 1

// Placeholder names come from the workload spec (DELTA, SEGMENT, ...), and are
// kept verbatim so the struct fields read like the SQL they bind.
#![allow(dead_code, non_snake_case)]

use synno_rt::args::ArgScanner;
use synno_rt::Result;

${query_structs_and_parsers}
