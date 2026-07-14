//! Query ${qid}. THIS IS YOUR FILE.

use engine_builder::Database;
use synno_rt::prelude::*;

use crate::args::Q${qid}Args;

// SQL:
// ${query_sql}

pub fn run_q${qid}(db: &Database, args: &Q${qid}Args) -> Result<RecordBatch> {
    let _ = (db, args);

    // TODO: implement the query, accumulating each output column into a typed Vec.
    //
    // Parallel-ready shape: the shared query pool is already wired up. Non-parallel
    // validation runs with CORE_IDS=1, so parallel_for/parallel_reduce take the serial
    // fast path; the same code must stay correct when CORE_IDS is raised later. Do not
    // write separate ST/MT code paths. Prefer writing the dominant scan/aggregation
    // through parallel_reduce now:
    //
    //   const MORSEL: usize = 1 << 16;
    //   let n_rows = /* logical rows in the dominant scanned table */;
    //   let n_morsels = (n_rows + MORSEL - 1) / MORSEL;
    //   type Acc = /* scalar, array, Vec, or map of exact integer/fixed-point state */;
    //   let acc = parallel_reduce(n_morsels, Acc::default(),
    //       |mut local, m| {
    //           let lo = m * MORSEL;
    //           let hi = (lo + MORSEL).min(n_rows);
    //           for row in lo..hi {
    //               // read db's columns, apply filters/joins, update `local` only
    //           }
    //           local
    //       },
    //       |mut a, b| { /* deterministic merge: +, min, max, bit-or, bucket add */ a });
    //
    // Guardrails: partition by logical row ranges; build shared lookup/hash structures
    // before the parallel region and treat them as read-only inside it; never mutate a
    // shared hot-loop map, output vector, or counter; do not nest pool calls.
    //
    // CRITICAL for exactness: a DECIMAL output column (a SUM of decimals, or a decimal
    // expression) MUST be accumulated as the exact unscaled integer in i128 - never
    // through f64 - and emitted with decimal_column(&values, precision, scale). The scale
    // is DuckDB's column scale (SUM(l_extendedprice) -> DECIMAL(38,2), scale 2; a product
    // of two DECIMAL(_,2) -> scale 4). A HUGEINT output is the same exact-integer family:
    // hugeint_column(&values). Only genuinely DOUBLE columns (AVG, ...) use double_column.
    //
    // Pick the builder by the output column's value FAMILY; egress emits the exact
    // DuckDB/Arrow type, failing loudly if a value does not fit:
    //   decimal_column(&v, precision, scale)  DECIMAL - exact, from i128
    //   hugeint_column(&v)                    HUGEINT - exact, from i128
    //   integer_column(&v, &valid, target)    signed/unsigned integers of any width
    //   uint64_column(&v, &valid, target)     UBIGINT above i64::MAX
    //   double_column(&v, &valid, target)     DOUBLE
    //   string_column(&v, &valid, target)     VARCHAR
    //   bool_column(&v, &valid)               BOOLEAN
    //   date_column(&v, &valid, target)       DATE (i32 days since 1970-01-01)
    //
    // NULLs: pass a Validity mask (valid[i] == 0 -> NULL at row i) when a result column
    // can be NULL (a LEFT JOIN miss, MIN/MAX/AVG over an empty group, NULLIF, a
    // NULL-propagating expression). Never substitute 0 / "" / the epoch for a NULL. An
    // empty Validity means "no nulls".
    //
    // e.g.:
    //   let no_nulls: Validity = Vec::new();
    //   make_table(vec![
    //       ("l_returnflag", string_column(&l_returnflag, &no_nulls, None)?),
    //       ("sum_qty",      decimal_column(&sum_qty, 38, 2, &no_nulls)?),
    //       ("avg_qty",      double_column(&avg_qty, &no_nulls, None)?),
    //   ])

    make_table(vec![]) // TODO: build and return the real result
}
