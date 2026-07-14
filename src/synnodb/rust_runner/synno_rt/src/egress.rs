//! Build the query's Arrow result, reproducing DuckDB's type AND value exactly.
//!
//! The Rust counterpart of `cpp_helpers/column_egress.hpp`, checked against the
//! same table of cases by `tests/test_column_egress.py`. The correctness gate
//! compares DECIMAL/INT/DATE/STRING/BOOL/TIMESTAMP for EXACT equality (only
//! DOUBLE is tolerant) and treats NULL as distinct from any value, so a decimal
//! routed through f64, a wrong type, or a NULL emitted as 0 all fail.

use std::sync::Arc;

use arrow::array::{
    Array, ArrayRef, BooleanArray, Date32Array, Decimal128Array, Decimal256Array, Float64Array,
    Int64Array, StringArray, UInt64Array,
};
use arrow::compute::kernels::cast::cast_with_options;
use arrow::compute::CastOptions;
use arrow::datatypes::{i256, DataType, Field, Schema};
use arrow::record_batch::RecordBatch;

use crate::{Error, Result};

/// Per-row validity: `valid[i] == 0` marks row i NULL. Empty means "no nulls",
/// so a column with no nullable rows needs no mask.
pub type Validity = Vec<u8>;

/// Errors on an impossible or overflowing cast rather than nulling the value.
/// See the note in `ingest.rs`: arrow-rs's `safe: true` means "null on failure",
/// which would turn an overflow into a silently wrong result.
const CAST_ERRS_ON_OVERFLOW: CastOptions<'static> = CastOptions {
    safe: false,
    format_options: arrow::util::display::FormatOptions::new(),
};

fn is_null(valid: &Validity, i: usize) -> bool {
    !valid.is_empty() && valid[i] == 0
}

fn check_validity(n: usize, valid: &Validity, what: &str) -> Result<()> {
    if !valid.is_empty() && valid.len() != n {
        return Err(Error::new(format!(
            "column_egress: {what} validity has {} entries but the column has {n} rows",
            valid.len()
        )));
    }
    Ok(())
}

fn cast_to(arr: ArrayRef, target: Option<&DataType>) -> Result<ArrayRef> {
    match target {
        None => Ok(arr),
        Some(t) if arr.data_type() == t => Ok(arr),
        Some(t) => cast_with_options(arr.as_ref(), t, &CAST_ERRS_ON_OVERFLOW).map_err(|e| {
            Error::new(format!(
                "column_egress: cannot cast {} to {t}: {e}",
                arr.data_type()
            ))
        }),
    }
}

/// Build an option-iterator honouring the validity mask, for the array builders.
fn opt<'a, T: Copy + 'a>(
    values: &'a [T],
    valid: &'a Validity,
) -> impl Iterator<Item = Option<T>> + 'a {
    values
        .iter()
        .enumerate()
        .map(move |(i, v)| if is_null(valid, i) { None } else { Some(*v) })
}

// ---- integers ---------------------------------------------------------------

/// BIGINT canonical, cast to any narrower/other integer target (INTEGER,
/// SMALLINT, ...). Pass `target` when DuckDB's output type is narrower than i64.
pub fn int64_column(
    values: &[i64],
    valid: &Validity,
    target: Option<&DataType>,
) -> Result<ArrayRef> {
    check_validity(values.len(), valid, "int64")?;
    let arr: ArrayRef = Arc::new(Int64Array::from_iter(opt(values, valid)));
    cast_to(arr, target)
}

/// UBIGINT canonical: for values that may exceed i64::MAX.
pub fn uint64_column(
    values: &[u64],
    valid: &Validity,
    target: Option<&DataType>,
) -> Result<ArrayRef> {
    check_validity(values.len(), valid, "uint64")?;
    let arr: ArrayRef = Arc::new(UInt64Array::from_iter(opt(values, valid)));
    cast_to(arr, target)
}

/// Accept the narrow integer vector the storage plan chose (u8 codes, i32 keys,
/// ...) and emit the output column's exact type. Widening to i64 first keeps one
/// code path; the cast back down is range-checked.
pub fn integer_column<T>(
    values: &[T],
    valid: &Validity,
    target: Option<&DataType>,
) -> Result<ArrayRef>
where
    T: Copy + Into<i64>,
{
    let widened: Vec<i64> = values.iter().map(|v| (*v).into()).collect();
    int64_column(&widened, valid, target)
}

// ---- floating ---------------------------------------------------------------

/// Only genuinely DOUBLE columns (AVG, ...) belong here. A DECIMAL routed
/// through f64 fails the exactness gate.
pub fn double_column(
    values: &[f64],
    valid: &Validity,
    target: Option<&DataType>,
) -> Result<ArrayRef> {
    check_validity(values.len(), valid, "double")?;
    let arr: ArrayRef = Arc::new(Float64Array::from_iter(opt(values, valid)));
    cast_to(arr, target)
}

// ---- bool / string / date ---------------------------------------------------

pub fn bool_column(values: &[bool], valid: &Validity) -> Result<ArrayRef> {
    check_validity(values.len(), valid, "bool")?;
    Ok(Arc::new(BooleanArray::from_iter(opt(values, valid))))
}

pub fn string_column(
    values: &[String],
    valid: &Validity,
    target: Option<&DataType>,
) -> Result<ArrayRef> {
    check_validity(values.len(), valid, "string")?;
    let arr: ArrayRef = Arc::new(StringArray::from_iter(values.iter().enumerate().map(
        |(i, v)| {
            if is_null(valid, i) {
                None
            } else {
                Some(v.as_str())
            }
        },
    )));
    cast_to(arr, target)
}

/// DATE from i32 days since 1970-01-01 (DuckDB's DATE / Arrow date32).
pub fn date_column(
    days: &[i32],
    valid: &Validity,
    target: Option<&DataType>,
) -> Result<ArrayRef> {
    check_validity(days.len(), valid, "date")?;
    let arr: ArrayRef = Arc::new(Date32Array::from_iter(opt(days, valid)));
    cast_to(arr, target)
}

// ---- decimal ----------------------------------------------------------------

fn pow10_i128(p: u8) -> i128 {
    let mut out: i128 = 1;
    for _ in 0..p {
        out *= 10;
    }
    out
}

/// DECIMAL(precision, scale) built EXACTLY from the unscaled i128 accumulator.
/// No float anywhere: the i128 IS the decimal's unscaled value.
///
/// The decimal builder does not enforce the declared precision, so it is the one
/// egress path that bypasses Cast's range check: an accumulator that overflowed
/// DECIMAL(precision) would be emitted as an out-of-range value rather than
/// failing. Guard it explicitly -- |value| must be < 10^precision.
pub fn decimal_column(
    values: &[i128],
    precision: u8,
    scale: i8,
    valid: &Validity,
) -> Result<ArrayRef> {
    check_validity(values.len(), valid, "decimal")?;

    if precision <= 38 {
        let bound = pow10_i128(precision);
        for (i, v) in values.iter().enumerate() {
            if is_null(valid, i) {
                continue;
            }
            if v.unsigned_abs() >= bound.unsigned_abs() {
                return Err(Error::new(format!(
                    "column_egress: decimal value {v} does not fit DECIMAL({precision},{scale}) \
                     - the accumulator overflowed the column's precision"
                )));
            }
        }
        let arr = Decimal128Array::from_iter(opt(values, valid))
            .with_precision_and_scale(precision, scale)
            .map_err(|e| Error::new(format!("column_egress: decimal128: {e}")))?;
        return Ok(Arc::new(arr));
    }

    // precision > 38 -> decimal256, still straight from the i128 (sign-extended).
    let arr = Decimal256Array::from_iter(
        values
            .iter()
            .enumerate()
            .map(|(i, v)| if is_null(valid, i) { None } else { Some(i256::from_i128(*v)) }),
    )
    .with_precision_and_scale(precision, scale)
    .map_err(|e| Error::new(format!("column_egress: decimal256: {e}")))?;
    Ok(Arc::new(arr))
}

/// DuckDB exports HUGEINT through Arrow as decimal128(38,0). It is an exact
/// integer family, not floating point and not a formatted string.
pub fn hugeint_column(values: &[i128], valid: &Validity) -> Result<ArrayRef> {
    decimal_column(values, 38, 0, valid)
}

// ---- table ------------------------------------------------------------------

/// Assemble the result in DuckDB's column order.
///
/// Every output column must have the same number of rows. RecordBatch would
/// reject a mismatch with a message that does not say which column is at fault;
/// a generation bug (filling one column's vector but not another's) is common
/// enough to be worth naming the offender.
pub fn make_table(columns: Vec<(&str, ArrayRef)>) -> Result<RecordBatch> {
    let mut nrows: Option<usize> = None;
    for (name, arr) in &columns {
        match nrows {
            None => nrows = Some(arr.len()),
            Some(n) if arr.len() != n => {
                return Err(Error::new(format!(
                    "column_egress: make_table column '{name}' has {} rows but the result has {n}; \
                     every output column must have the same number of rows",
                    arr.len()
                )))
            }
            _ => {}
        }
    }

    let fields: Vec<Field> = columns
        .iter()
        .map(|(name, arr)| Field::new(*name, arr.data_type().clone(), true))
        .collect();
    let arrays: Vec<ArrayRef> = columns.into_iter().map(|(_, a)| a).collect();

    RecordBatch::try_new(Arc::new(Schema::new(fields)), arrays)
        .map_err(|e| Error::new(format!("column_egress: make_table: {e}")))
}
