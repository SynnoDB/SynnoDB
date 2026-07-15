//! The Rust half of the cross-language conformance harness.
//!
//! Mirrors `tests/cpp/column_ingest_test.cpp` mode for mode and key for key: the
//! same modes (`lineitem <parquet>`, `synth <parquet>`, `nullable`) printing the
//! same `key=value` line, so `tests/test_column_ingest.py` can run one table of
//! assertions against both runtimes and fail on any divergence.
//!
//! Keep the printed keys in lockstep with the C++ driver. If they drift apart,
//! the test silently stops comparing the two.

use std::fs::File;
use std::sync::Arc;

use arrow::array::{Array, Int64Array};
use arrow::datatypes::{DataType, Field, Schema};
use arrow::record_batch::RecordBatch;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;

use synno_rt::egress;
use synno_rt::ingest::{self, Validity};

/// Read the parquet as ONE RecordBatch. The C++ driver reads an arrow::Table
/// (chunked); the helpers must produce identical values either way, so reading
/// it whole here is the point, not a shortcut.
fn read_parquet(path: &str) -> RecordBatch {
    let file = File::open(path).unwrap_or_else(|e| panic!("open {path}: {e}"));
    let builder = ParquetRecordBatchReaderBuilder::try_new(file).expect("parquet reader");
    let schema = builder.schema().clone();
    let batches: Vec<RecordBatch> = builder
        .build()
        .expect("parquet build")
        .collect::<std::result::Result<_, _>>()
        .expect("parquet read");
    arrow::compute::concat_batches(&schema, &batches).expect("concat")
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: <lineitem|synth|nullable> [parquet]");
        std::process::exit(2);
    }

    match args[1].as_str() {
        "nullable" => nullable(),
        "lineitem" => lineitem(&args[2]),
        "synth" => synth(&args[2]),
        "shm-read" => shm_read(&args[2]),
        m => {
            eprintln!("unknown mode: {m}");
            std::process::exit(2);
        }
    }
}

/// Map a /dev/shm Arrow-IPC segment via the same zero-copy path a Rust engine's
/// loader uses, and print the same line the C++ driver's `read` mode prints
/// (tests/cpp/shm_io_test.cpp) so one assertion grades both. The first column is
/// int64 (the test writes it that way), summed to prove values survive the map.
fn shm_read(path: &str) {
    use arrow::array::Int64Array;

    let batch = synno_rt::shm::read_table(path).expect("shm read");
    let col0 = batch.column(0);
    let a = col0
        .as_any()
        .downcast_ref::<Int64Array>()
        .expect("col0 is int64");
    let mut sum: i64 = 0;
    for i in 0..a.len() {
        if !a.is_null(i) {
            sum += a.value(i);
        }
    }
    println!(
        "rows={} cols={} col0={} sum0={sum}",
        batch.num_rows(),
        batch.num_columns(),
        batch.schema().field(0).name(),
    );
}

/// In-memory nullable column [10, NULL, 20]. Ingest records the null in the
/// validity mask; egress re-emits it as a real Arrow NULL. The default (dense)
/// path still reads the null as 0.
fn nullable() {
    let arr = Int64Array::from(vec![Some(10i64), None, Some(20)]);
    let schema = Arc::new(Schema::new(vec![Field::new("n", DataType::Int64, true)]));
    let batch = RecordBatch::try_new(schema, vec![Arc::new(arr)]).unwrap();

    let nn = ingest::as_integer_nullable::<i64>(&batch, "n").unwrap();
    let dense = ingest::as_integer::<i64>(&batch, "n").unwrap();
    let out = egress::int64_column(&nn.values, &nn.valid, None).unwrap();

    println!(
        "validity={}{}{} dense1={} egress_nulls={} egress_isnull1={} egress_isnull0={}",
        nn.valid[0],
        nn.valid[1],
        nn.valid[2],
        dense[1],
        out.null_count(),
        if out.is_null(1) { 1 } else { 0 },
        if out.is_null(0) { 1 } else { 0 },
    );
}

fn lineitem(path: &str) {
    let t = read_parquet(path);
    let qty = ingest::scaled_integer::<i64>(&t, "l_quantity", 2).unwrap();
    let ep = ingest::scaled_integer::<i64>(&t, "l_extendedprice", 2).unwrap();
    let okey = ingest::as_integer::<i64>(&t, "l_orderkey").unwrap();
    let rf = ingest::as_string(&t, "l_returnflag").unwrap();
    let sd = ingest::as_date_days(&t, "l_shipdate").unwrap();

    let sq: i64 = qty.iter().sum();
    let se: i64 = ep.iter().sum();
    let so: i64 = okey.iter().sum();
    let ca = rf.iter().filter(|s| s.as_str() == "A").count();
    let mn = sd.iter().copied().min().unwrap_or(i32::MAX);
    let mx = sd.iter().copied().max().unwrap_or(i32::MIN);

    println!(
        "rows={} sum_qty={sq} sum_ep={se} sum_okey={so} rf_A={ca} sd_min={mn} sd_max={mx}",
        t.num_rows()
    );
}

fn synth(path: &str) {
    let t = read_parquet(path);
    let dec = ingest::scaled_integer::<i64>(&t, "dec_col", 2).unwrap();
    let dec16 = ingest::scaled_integer::<i16>(&t, "dec_col", 2).unwrap();
    let iv = ingest::as_integer::<i16>(&t, "int_col").unwrap();
    let bv = ingest::as_integer::<u8>(&t, "bool_col").unwrap();
    let sv = ingest::as_string(&t, "dict_col").unwrap();
    let tv = ingest::as_date_days(&t, "ts_col").unwrap();
    let dv = ingest::as_date_days(&t, "date_col").unwrap();
    let fv = ingest::as_double(&t, "dbl_col").unwrap();

    let sdec: i64 = dec.iter().sum();
    let sdec16: i64 = dec16.iter().map(|v| *v as i64).sum();
    let siv: i64 = iv.iter().map(|v| *v as i64).sum();
    let sbv: i64 = bv.iter().map(|v| *v as i64).sum();
    let ca = sv.iter().filter(|s| s.as_str() == "A").count();
    let sf: f64 = fv.iter().sum();

    // The C++ driver prints the double with iostream's default 6 significant
    // digits; match that so the parsed values compare equal.
    println!(
        "dec={sdec} dec16={sdec16} int={siv} bool={sbv} dictA={ca} ts0={} date0={} dbl={}",
        tv[0],
        dv[0],
        format_g6(sf),
    );

    // Exercise the exact-decimal egress path the correctness gate depends on:
    // i128 unscaled -> decimal128, never through a float.
    let unscaled: Vec<i128> = dec.iter().map(|v| *v as i128).collect();
    let valid: Validity = Vec::new();
    let d = egress::decimal_column(&unscaled, 38, 2, &valid).unwrap();
    println!("egress_decimal_rows={} nulls={}", d.len(), d.null_count());
}

/// iostream's default float formatting: 6 significant digits, trailing zeros trimmed.
fn format_g6(v: f64) -> String {
    let s = format!("{:.6}", v);
    let s = s.trim_end_matches('0').trim_end_matches('.');
    s.to_string()
}
