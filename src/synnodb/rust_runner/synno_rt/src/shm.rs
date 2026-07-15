//! Zero-copy Arrow ingest from a `/dev/shm` segment (the hot-load plane).
//!
//! The Rust counterpart of `cpp_helpers/shm_paths.hpp` + `shm_arrow_loader.hpp`.
//! The Python parent (`router/shm_transport.py::write_arrow_segments`) writes each
//! table as an Arrow-IPC **file** to `<SYNNODB_SHM_INGEST>/<table>.arrow`; the
//! generated loader maps it here and gets a `RecordBatch` whose arrays are
//! **views into the mapping** -- no disk read, no parquet decode, no copy. Both
//! ends share one wire format, pinned by `tests/test_cpp_shm.py`.
//!
//! The two planes share one binary: the loader consults `ingest_enabled()` and
//! reads shm when `SYNNODB_SHM_INGEST` is set, otherwise parquet.

use std::fs::File;
use std::ptr::NonNull;
use std::sync::Arc;

use arrow::array::RecordBatch;
use arrow::buffer::Buffer;
use arrow::compute::concat_batches;
use arrow::datatypes::Schema;
use arrow::ipc::convert::fb_to_schema;
use arrow::ipc::reader::{read_footer_length, FileDecoder};
use arrow::ipc::root_as_footer;
use memmap2::Mmap;

use crate::{Error, Result};

const ENV: &str = "SYNNODB_SHM_INGEST";

/// The ingest directory from `SYNNODB_SHM_INGEST`, or `None` when the shm plane
/// is off. Mirrors `shm_paths.hpp::shm_ingest_dir` (empty == unset).
pub fn ingest_dir() -> Option<String> {
    match std::env::var(ENV) {
        Ok(v) if !v.is_empty() => Some(v),
        _ => None,
    }
}

pub fn ingest_enabled() -> bool {
    ingest_dir().is_some()
}

/// `<dir>/<table>.arrow` - the segment the Python parent wrote for this table.
/// Trailing-slash-safe, exactly like `shm_paths.hpp::shm_ingest_path_for`.
pub fn ingest_path_for(table: &str) -> String {
    let mut dir = ingest_dir().unwrap_or_default();
    if !dir.is_empty() && !dir.ends_with('/') {
        dir.push('/');
    }
    format!("{dir}{table}.arrow")
}

/// Map a `/dev/shm` Arrow-IPC-file segment read-only and return its single
/// `RecordBatch` **zero-copy**.
///
/// The mapping is adopted as an Arrow `Buffer` whose owner is the `Arc<Mmap>`, so
/// the arrays stay valid for the batch's lifetime and the munmap happens only
/// when the last array referencing it is dropped -- the Rust analogue of the C++
/// table keeping `MemoryMappedFile` alive transitively through its buffers.
///
/// Decoding uses `FileDecoder` with the default alignment policy: array data that
/// is already aligned (the common case for the numeric/string columns here) stays
/// a view into the mapping; only a misaligned buffer is copied to realign it.
/// Fails loudly on any error, as the C++ loader does -- the router treats an
/// engine exception as fallback-to-DuckDB.
pub fn read_table(path: &str) -> Result<Arc<RecordBatch>> {
    let file =
        File::open(path).map_err(|e| Error::new(format!("shm open failed for {path}: {e}")))?;
    // Safety: the file is a private, parent-owned /dev/shm segment held open for
    // the engine's lifetime; we map it read-only and never mutate it.
    let mmap = unsafe { Mmap::map(&file) }
        .map_err(|e| Error::new(format!("shm mmap failed for {path}: {e}")))?;

    let len = mmap.len();
    let ptr = NonNull::new(mmap.as_ptr() as *mut u8)
        .ok_or_else(|| Error::new(format!("shm map for {path} has a null base")))?;
    // Safety: `ptr`/`len` describe exactly the mapping owned by `owner`; mmap
    // addresses are stable, and `owner` keeps the mapping alive as long as the
    // Buffer (and any array sliced from it) lives.
    let owner: Arc<dyn arrow::alloc::Allocation> = Arc::new(mmap);
    let buffer = unsafe { Buffer::from_custom_allocation(ptr, len, owner) };

    read_ipc_file(&buffer, path)
}

/// Decode an Arrow-IPC *file* laid out in `buffer` (schema+dictionaries+batches
/// via the footer), zero-copy. Split out from the mapping so it can be unit
/// tested over an in-memory buffer.
fn read_ipc_file(buffer: &Buffer, path: &str) -> Result<Arc<RecordBatch>> {
    let bytes = buffer.as_slice();
    let len = bytes.len();
    if len < 10 {
        return Err(Error::new(format!("shm segment {path} is too small to be Arrow IPC")));
    }

    // Footer: the last 10 bytes are the footer length + magic; the footer flatbuffer
    // precedes them. (Same parse arrow's own FileReaderBuilder performs.)
    let footer_len_bytes: [u8; 10] = bytes[len - 10..]
        .try_into()
        .map_err(|_| Error::new(format!("shm segment {path}: bad footer trailer")))?;
    let footer_len = read_footer_length(footer_len_bytes)
        .map_err(|e| Error::new(format!("shm segment {path}: {e}")))?;
    let footer_start = len
        .checked_sub(10 + footer_len)
        .ok_or_else(|| Error::new(format!("shm segment {path}: footer length exceeds file")))?;

    let footer = root_as_footer(&bytes[footer_start..len - 10])
        .map_err(|e| Error::new(format!("shm segment {path}: unreadable footer: {e:?}")))?;
    let ipc_schema = footer
        .schema()
        .ok_or_else(|| Error::new(format!("shm segment {path}: footer has no schema")))?;
    let schema: Arc<Schema> = Arc::new(fb_to_schema(ipc_schema));

    // For each block, hand the decoder a sub-buffer that starts exactly at the
    // block (metadata+body): arrow's FileDecoder reads the message from the start
    // of the buffer it is given (its FileReader copies each block out with
    // read_block; we slice instead). `slice_with_length` shares the mapping and
    // its Arc<Mmap> owner, so the slice stays zero-copy, and its base is 8-aligned
    // (page-aligned mmap + 8-aligned block offset) so the flatbuffer parse aligns.
    let block_buf = |offset: i64, meta: i32, body: i64| -> Buffer {
        buffer.slice_with_length(offset as usize, (meta as i64 + body) as usize)
    };

    let mut decoder = FileDecoder::new(schema.clone(), footer.version());
    if let Some(dictionaries) = footer.dictionaries() {
        for block in dictionaries.iter() {
            let buf = block_buf(block.offset(), block.metaDataLength(), block.bodyLength());
            decoder
                .read_dictionary(&block, &buf)
                .map_err(|e| Error::new(format!("shm segment {path}: dictionary: {e}")))?;
        }
    }

    let mut batches: Vec<RecordBatch> = Vec::new();
    if let Some(record_batches) = footer.recordBatches() {
        for block in record_batches.iter() {
            let buf = block_buf(block.offset(), block.metaDataLength(), block.bodyLength());
            if let Some(rb) = decoder
                .read_record_batch(&block, &buf)
                .map_err(|e| Error::new(format!("shm segment {path}: record batch: {e}")))?
            {
                batches.push(rb);
            }
        }
    }

    // The builder API works on one RecordBatch per table (the parquet path also
    // concatenates). A single-batch segment -- the common DuckDB-export case --
    // is returned directly and stays zero-copy; multiple batches are concatenated
    // (one copy), matching the parquet path's shape.
    match batches.len() {
        0 => Ok(Arc::new(RecordBatch::new_empty(schema))),
        1 => Ok(Arc::new(batches.pop().unwrap())),
        _ => concat_batches(&schema, &batches)
            .map(Arc::new)
            .map_err(|e| Error::new(format!("shm segment {path}: concat: {e}"))),
    }
}
