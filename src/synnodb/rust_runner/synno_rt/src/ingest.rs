//! Read Arrow columns into typed Rust vectors.
//!
//! The Rust counterpart of `cpp_helpers/column_ingest.hpp`, and it must agree
//! with it value-for-value: both are checked against the same table of cases by
//! `tests/test_column_ingest.py`. Read that header before changing semantics
//! here -- a divergence produces an engine that is silently wrong on some
//! queries rather than one that fails to build.
//!
//! Each helper casts the column to a single canonical Arrow type and reads only
//! that type. Cast covers the full set of Arrow types, so there is no per-type
//! match to maintain; a cast Arrow cannot do -- or a value that overflows the
//! target -- is an error rather than a wrong number.
//!
//! NULLs read as the type's zero (0 / 0.0 / "" / epoch) by default: correct for
//! a NOT NULL column, and for SUM over a nullable one (adding zero == skipping).
//! For a query whose result depends on SQL null semantics (COUNT(col), AVG,
//! NULL-propagating arithmetic, a predicate on a nullable column, IS NULL), use
//! the `*_nullable` variant, which also returns the validity mask.

use std::sync::Arc;

use arrow::array::{
    Array, Date32Array, Decimal128Array, Float64Array, Int8Array, Int16Array, Int32Array,
    Int64Array, StringArray, UInt8Array, UInt16Array, UInt32Array, UInt64Array,
};
use arrow::compute::kernels::cast::cast_with_options;
use arrow::compute::CastOptions;
use arrow::datatypes::{DataType, TimeUnit};
use arrow::record_batch::RecordBatch;

use crate::{Error, Result};

/// Per-row validity, Arrow's valid_bytes convention: `valid[i] == 0` marks row i
/// NULL. The same type and convention as `egress::Validity`, so a mask read on
/// the way in can be carried straight back out.
pub type Validity = Vec<u8>;

/// A column plus the rows of it that are NULL.
pub struct Nullable<T> {
    pub values: Vec<T>,
    pub valid: Validity,
}

/// Arrow C++ `CastOptions::Safe()` errors on an impossible or overflowing cast.
/// arrow-rs inverts the name: `safe: true` would return NULL on failure, which
/// would turn an overflow into a silently wrong value. `safe: false` is the one
/// that errors, and is what matches the C++ contract.
const CAST_ERRS_ON_OVERFLOW: CastOptions<'static> = CastOptions {
    safe: false,
    format_options: arrow::util::display::FormatOptions::new(),
};

fn column<'a>(batch: &'a RecordBatch, name: &str) -> Result<&'a Arc<dyn Array>> {
    let idx = batch
        .schema()
        .index_of(name)
        .map_err(|_| Error::new(format!("column_ingest: column not found: {name}")))?;
    Ok(batch.column(idx))
}

/// Cast to `target`. Errors if Arrow cannot perform the cast.
fn canonicalize(col: &Arc<dyn Array>, target: &DataType) -> Result<Arc<dyn Array>> {
    if col.data_type() == target {
        return Ok(Arc::clone(col));
    }
    cast_with_options(col.as_ref(), target, &CAST_ERRS_ON_OVERFLOW).map_err(|e| {
        Error::new(format!(
            "column_ingest: cannot canonicalize column of type {}: {e}",
            col.data_type()
        ))
    })
}

/// The integer widths a storage plan may choose for a persisted column. The
/// point of the trait is that the plan picks the *narrowest correct* type
/// (`u8` for a small code domain, `i32` for a key), not that everything is i64.
pub trait IngestInt: Copy + Default {
    const ARROW_TYPE: DataType;
    fn read(array: &dyn Array, i: usize) -> Self;
    /// Narrow an i128 unscaled decimal, or fail if it does not fit.
    fn from_i128(v: i128, scale: i8) -> Result<Self>;
}

macro_rules! impl_ingest_int {
    ($t:ty, $arrow:expr, $arr:ty) => {
        impl IngestInt for $t {
            const ARROW_TYPE: DataType = $arrow;
            fn read(array: &dyn Array, i: usize) -> Self {
                array.as_any().downcast_ref::<$arr>().unwrap().value(i)
            }
            fn from_i128(v: i128, scale: i8) -> Result<Self> {
                <$t>::try_from(v).map_err(|_| {
                    Error::new(format!(
                        "column_ingest::scaled_integer: value {v} does not fit {} at scale={scale}",
                        stringify!($t)
                    ))
                })
            }
        }
    };
}

impl_ingest_int!(i8, DataType::Int8, Int8Array);
impl_ingest_int!(i16, DataType::Int16, Int16Array);
impl_ingest_int!(i32, DataType::Int32, Int32Array);
impl_ingest_int!(i64, DataType::Int64, Int64Array);
impl_ingest_int!(u8, DataType::UInt8, UInt8Array);
impl_ingest_int!(u16, DataType::UInt16, UInt16Array);
impl_ingest_int!(u32, DataType::UInt32, UInt32Array);
impl_ingest_int!(u64, DataType::UInt64, UInt64Array);

// ---- as_integer<T>: any integer/bool/date source -> the plan's integer T -----

pub fn as_integer<T: IngestInt>(batch: &RecordBatch, name: &str) -> Result<Vec<T>> {
    Ok(as_integer_nullable::<T>(batch, name)?.values)
}

pub fn as_integer_nullable<T: IngestInt>(
    batch: &RecordBatch,
    name: &str,
) -> Result<Nullable<T>> {
    let c = canonicalize(column(batch, name)?, &T::ARROW_TYPE)?;
    let n = c.len();
    let mut values = Vec::with_capacity(n);
    let mut valid = Vec::with_capacity(n);
    for i in 0..n {
        let is_null = c.is_null(i);
        valid.push(if is_null { 0 } else { 1 });
        values.push(if is_null { T::default() } else { T::read(c.as_ref(), i) });
    }
    Ok(Nullable { values, valid })
}

// ---- as_double: any numeric/decimal source -> f64 ----------------------------

pub fn as_double(batch: &RecordBatch, name: &str) -> Result<Vec<f64>> {
    Ok(as_double_nullable(batch, name)?.values)
}

pub fn as_double_nullable(batch: &RecordBatch, name: &str) -> Result<Nullable<f64>> {
    let c = canonicalize(column(batch, name)?, &DataType::Float64)?;
    let a = c.as_any().downcast_ref::<Float64Array>().unwrap();
    let mut values = Vec::with_capacity(a.len());
    let mut valid = Vec::with_capacity(a.len());
    for i in 0..a.len() {
        let is_null = a.is_null(i);
        valid.push(if is_null { 0 } else { 1 });
        values.push(if is_null { 0.0 } else { a.value(i) });
    }
    Ok(Nullable { values, valid })
}

// ---- scaled_integer<T>: decimal source -> fixed-point integer (value*10^scale)

/// Read each value's unscaled integer at `scale`. A value that does not fit `T`
/// at this scale is an error rather than a truncation, so the plan can keep
/// decimal/money/quantity columns narrow when their domain allows it.
///
/// Fast path: a decimal128 source ALREADY at `scale` is read in place -- its
/// unscaled integer is already value*10^scale, so casting would copy 16
/// bytes/row without changing a single value. Any other source (different
/// scale, integer, float) still goes through Cast, so the helper stays
/// universal and exact.
pub fn scaled_integer<T: IngestInt>(
    batch: &RecordBatch,
    name: &str,
    scale: i8,
) -> Result<Vec<T>> {
    Ok(scaled_integer_nullable::<T>(batch, name, scale)?.values)
}

pub fn scaled_integer_nullable<T: IngestInt>(
    batch: &RecordBatch,
    name: &str,
    scale: i8,
) -> Result<Nullable<T>> {
    let col = column(batch, name)?;
    let native_scale = matches!(
        col.data_type(),
        DataType::Decimal128(_, s) if *s == scale
    );
    let c = if native_scale {
        Arc::clone(col)
    } else {
        canonicalize(col, &DataType::Decimal128(38, scale))?
    };

    let a = c.as_any().downcast_ref::<Decimal128Array>().unwrap();
    let mut values = Vec::with_capacity(a.len());
    let mut valid = Vec::with_capacity(a.len());
    for i in 0..a.len() {
        if a.is_null(i) {
            valid.push(0);
            values.push(T::default());
            continue;
        }
        valid.push(1);
        values.push(T::from_i128(a.value(i), scale)?);
    }
    Ok(Nullable { values, valid })
}

// ---- as_string: any string/dictionary source -> String -----------------------

pub fn as_string(batch: &RecordBatch, name: &str) -> Result<Vec<String>> {
    Ok(as_string_nullable(batch, name)?.values)
}

pub fn as_string_nullable(batch: &RecordBatch, name: &str) -> Result<Nullable<String>> {
    let c = canonicalize(column(batch, name)?, &DataType::Utf8)?;
    let a = c.as_any().downcast_ref::<StringArray>().unwrap();
    let mut values = Vec::with_capacity(a.len());
    let mut valid = Vec::with_capacity(a.len());
    for i in 0..a.len() {
        let is_null = a.is_null(i);
        valid.push(if is_null { 0 } else { 1 });
        values.push(if is_null { String::new() } else { a.value(i).to_string() });
    }
    Ok(Nullable { values, valid })
}

// ---- as_date_days: any date/timestamp source -> i32 days since 1970-01-01 -----

pub fn as_date_days(batch: &RecordBatch, name: &str) -> Result<Vec<i32>> {
    Ok(as_date_days_nullable(batch, name)?.values)
}

pub fn as_date_days_nullable(batch: &RecordBatch, name: &str) -> Result<Nullable<i32>> {
    let col = column(batch, name)?;
    // A timestamp casts to date32 only via an intermediate date64 in arrow-rs;
    // going straight from Timestamp to Date32 is not a supported kernel.
    let col = if matches!(col.data_type(), DataType::Timestamp(_, _)) {
        canonicalize(col, &DataType::Date64)?
    } else {
        Arc::clone(col)
    };
    let c = canonicalize(&col, &DataType::Date32)?;
    let a = c.as_any().downcast_ref::<Date32Array>().unwrap();
    let mut values = Vec::with_capacity(a.len());
    let mut valid = Vec::with_capacity(a.len());
    for i in 0..a.len() {
        let is_null = a.is_null(i);
        valid.push(if is_null { 0 } else { 1 });
        values.push(if is_null { 0 } else { a.value(i) });
    }
    Ok(Nullable { values, valid })
}

/// Kept so a storage plan can name the unit explicitly; TimeUnit is otherwise
/// unused here, and this keeps the import honest.
pub const DATE_UNIT: TimeUnit = TimeUnit::Millisecond;
