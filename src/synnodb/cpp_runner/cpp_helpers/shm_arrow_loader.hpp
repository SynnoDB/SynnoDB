#pragma once
// ============================================================================
// shm_arrow_loader.hpp — zero-copy Arrow ingestion from a /dev/shm segment.
//
// This is the C++ engine-worker side of the Phase-3 shared-memory data plane
// (the Python side is src/synnodb/router/shm_transport.py + _worker_main.py).
// The Python parent writes a table as Arrow IPC into a /dev/shm segment; the
// engine maps it here and gets an arrow::Table whose buffers are ZERO-COPY views
// into the mapping (Arrow IPC is offset-based / position-independent, so no fixed
// mmap address is needed — unlike misc/misc/shm_test.cpp).
//
// ---------------------------------------------------------------------------
// STATUS: written against the documented Arrow C++ APIs (verified present in the
//   linked libarrow 23.0.1 — see compiler/compiler_factory.py:87 `pkgconfig_libs
//   = ["arrow","parquet"]`), but NOT YET COMPILED in this repo. It is intentionally
//   not in any build source list, so it cannot break the existing build.
//
// INTEGRATION (3 steps), grounded in the loader path:
//   1. Add this file's translation unit (or include it) to the loader lib sources
//      in compiler/compiler_factory_olap.py (alongside loader_api.cpp,
//      parquet_reader.cpp, loader_utils.cpp), and `#include <arrow/ipc/reader.h>`.
//   2. In the generated load() — prepare_repo/templates/parquet_reader.cpp:22-29 —
//      replace each `tables->X = ReadParquetTable(path + "X.parquet");` with
//      `tables->X = ReadArrowTableFromShm(shm_name_for("X"));` where the shm names
//      arrive via env (mirroring STORAGE_DIR; see workload_provider_olap.py:160-161).
//   3. Keep the mapping alive for the table's lifetime (ParquetTables already owns
//      the arrow::Table, so this is automatic).
//
// VALIDATION REQUIRED: compile with the engine's toolchain and round-trip a table
//   written by src/synnodb/router/shm_transport.py::ShmWriter.write_table (Arrow
//   IPC *file* format) — that Python writer is the tested reference producer.
// ============================================================================

#include <memory>
#include <stdexcept>
#include <string>

#include <arrow/io/file.h>
#include <arrow/ipc/reader.h>
#include <arrow/table.h>

namespace synnodb {

// Map a /dev/shm Arrow-IPC-file segment read-only and return its table zero-copy.
// Throws std::runtime_error on any failure (the Python router treats an engine
// exception as a fallback-to-DuckDB, so failing loudly here is correct).
inline std::shared_ptr<arrow::Table> ReadArrowTableFromShm(const std::string& shm_path) {
    auto file_result = arrow::io::MemoryMappedFile::Open(shm_path, arrow::io::FileMode::READ);
    if (!file_result.ok()) {
        throw std::runtime_error("shm map failed for " + shm_path + ": " +
                                 file_result.status().ToString());
    }
    std::shared_ptr<arrow::io::MemoryMappedFile> mapped = file_result.ValueOrDie();

    auto reader_result = arrow::ipc::RecordBatchFileReader::Open(mapped);
    if (!reader_result.ok()) {
        throw std::runtime_error("arrow ipc open failed for " + shm_path + ": " +
                                 reader_result.status().ToString());
    }
    std::shared_ptr<arrow::ipc::RecordBatchFileReader> reader = reader_result.ValueOrDie();

    auto table_result = reader->ToTable();
    if (!table_result.ok()) {
        throw std::runtime_error("arrow ipc->table failed for " + shm_path + ": " +
                                 table_result.status().ToString());
    }
    // Buffers are views into the mmap; the returned table keeps `mapped` alive
    // transitively through its buffers.
    return table_result.ValueOrDie();
}

}  // namespace synnodb
