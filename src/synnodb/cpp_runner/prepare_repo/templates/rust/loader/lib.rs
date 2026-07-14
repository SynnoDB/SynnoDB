//! The loader plugin: parquet -> Arrow tables.
//!
//! Read-only scaffold (the Rust counterpart of parquet_reader.hpp/.cpp). The
//! table fields and their reads are generated from the workload's schema; you do
//! not edit this file.
//!
//! `ParquetTables` is what the builder receives. It is `pub` and this crate is
//! also an rlib, so `builder` depends on it for the type -- the same way the C++
//! engine shares the struct through parquet_reader.hpp.

use std::fs::File;
use std::sync::Arc;

use arrow::record_batch::RecordBatch;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use synno_rt::Error;

/// One Arrow table per dataset table. Shared with the builder as an opaque
/// handle across the plugin ABI; the host never looks inside it.
pub struct ParquetTables {
    // start: table-defs
    // end: table-defs
}

/// Read a parquet file into a single Arrow table.
fn read_parquet_table(path: &str) -> Result<Arc<RecordBatch>, Error> {
    let file =
        File::open(path).map_err(|e| Error::new(format!("loader: cannot open {path}: {e}")))?;
    let builder = ParquetRecordBatchReaderBuilder::try_new(file)
        .map_err(|e| Error::new(format!("loader: {path}: {e}")))?;
    let schema = builder.schema().clone();
    let batches: Vec<RecordBatch> = builder
        .build()
        .map_err(|e| Error::new(format!("loader: {path}: {e}")))?
        .collect::<Result<_, _>>()
        .map_err(|e| Error::new(format!("loader: {path}: {e}")))?;
    let table = arrow::compute::concat_batches(&schema, &batches)
        .map_err(|e| Error::new(format!("loader: {path}: {e}")))?;
    Ok(Arc::new(table))
}

pub fn load_tables(dir: &str) -> Result<Box<ParquetTables>, Error> {
    let path = if dir.ends_with('/') {
        dir.to_string()
    } else {
        format!("{dir}/")
    };
    let _ = &path;

    Ok(Box::new(ParquetTables {
        // start: table-reads
        // end: table-reads
    }))
}
