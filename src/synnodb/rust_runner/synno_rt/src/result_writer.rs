//! Write a query's result where Python reads it.
//!
//! The Rust counterpart of `cpp_helpers/result_writer.hpp`. The contract is the
//! file, not the language: an Arrow IPC *file* at
//! `$SYNNODB_RESULT_DIR/result_<req_id>.arrow`, which
//! `router/process_engine.py::_read_arrow` opens with `pa.ipc.open_file`. Get
//! the path or the IPC framing wrong and the engine looks like it produced no
//! result at all.

use std::fs::File;
use std::path::PathBuf;

use arrow::ipc::writer::FileWriter;
use arrow::record_batch::RecordBatch;

use crate::{Error, Result};

/// Where results go. Set per-run by the Python side over the control pipe.
fn result_dir() -> Result<PathBuf> {
    std::env::var("SYNNODB_RESULT_DIR")
        .map(PathBuf::from)
        .map_err(|_| Error::new("SYNNODB_RESULT_DIR is not set".to_string()))
}

/// Write `table` as the result of request `req_id`.
///
/// Written to a temporary file and renamed, so a reader never observes a
/// half-written result: the rename is atomic within the directory.
pub fn write_result(table: &RecordBatch, req_id: &str) -> Result<()> {
    let dir = result_dir()?;
    let final_path = dir.join(format!("result_{req_id}.arrow"));
    let tmp_path = dir.join(format!(".result_{req_id}.arrow.tmp"));

    {
        let file = File::create(&tmp_path).map_err(|e| {
            Error::new(format!("result_writer: cannot create {}: {e}", tmp_path.display()))
        })?;
        let mut writer = FileWriter::try_new(file, &table.schema())
            .map_err(|e| Error::new(format!("result_writer: {e}")))?;
        writer
            .write(table)
            .map_err(|e| Error::new(format!("result_writer: write: {e}")))?;
        writer
            .finish()
            .map_err(|e| Error::new(format!("result_writer: finish: {e}")))?;
    }

    std::fs::rename(&tmp_path, &final_path).map_err(|e| {
        Error::new(format!(
            "result_writer: cannot rename into {}: {e}",
            final_path.display()
        ))
    })
}
