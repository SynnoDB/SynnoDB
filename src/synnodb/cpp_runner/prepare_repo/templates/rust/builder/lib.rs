//! The builder plugin: Arrow tables -> the engine's in-memory storage layout.
//!
//! THIS IS YOUR FILE. `Database` is the storage layout from the storage plan, and
//! `build` populates it. The query files read it; nothing else does.
//!
//! `Database` is `pub` and this crate is also an rlib, so the `query` crate
//! depends on it for the type -- the same way the C++ engine shares `struct
//! Database` through db_loader.hpp.

use engine_loader::ParquetTables;
use synno_rt::Result;

/// The in-memory representation of the data, laid out for these queries.
///
/// Rust owns everything here: if `build` returns an error part-way through, the
/// partial `Database` is dropped and every allocation it holds is freed. There is
/// no raw owning pointer to leak.
pub struct Database {
    // TODO: the columns and acceleration structures from the storage plan.
    // Struct-of-arrays: one Vec per column, in the narrowest correct element type
    // (u8/i16/i32/... for integers and codes, i64 fixed-point for decimals).
}

/// Populate the storage layout from the loader's Arrow tables.
///
/// Compose the `synno_rt::ingest` helpers -- do NOT decode Arrow buffers by hand.
/// They delegate to Arrow's cast, so every physical representation is handled and
/// an overflow is an error rather than a silently wrong number:
///
///   use synno_rt::prelude::*;
///   let l_quantity      = scaled_integer::<i16>(&tables.lineitem, "l_quantity", 2)?;
///   let l_orderkey      = as_integer::<i32>(&tables.lineitem, "l_orderkey")?;
///   let l_returnflag    = as_string(&tables.lineitem, "l_returnflag")?;
///   let l_shipdate_days = as_date_days(&tables.lineitem, "l_shipdate")?;
pub fn build(tables: &ParquetTables) -> Result<Database> {
    let _ = tables;

    // TODO: read the columns the queries need and build the storage layout.
    Ok(Database {})
}
