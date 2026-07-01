#pragma once
// ============================================================================
// shm_arrow_loader.hpp — zero-copy Arrow ingestion from a /dev/shm segment.
//
// The C++ engine ingest side of the shared-memory data plane (the Python side is
// router/shm_transport.py and router/process_engine.py::ShmHotLoadEngine). The Python
// parent writes each table as Arrow IPC into a /dev/shm segment; the engine maps it here
// and gets an arrow::Table whose buffers are ZERO-COPY views into the mapping (Arrow IPC is
// offset-based / position-independent, so no fixed mmap address is needed).
//
// WIRED & VALIDATED: the in-memory loader's generated load() takes the shm branch when
//   SYNNODB_SHM_INGEST is set — prepare_repo/prepare_workspace_olap.py::_gen_table_reads emits
//   `if (synnodb::shm_ingest_enabled()) { tables->X = ReadArrowTableFromShm(shm_ingest_path_for("X")); }`
//   into parquet_reader.cpp, which #includes this header; it is on the compiler include path
//   (compiler/compiler_factory.py include_dirs) and links libarrow. Round-tripped against the
//   Python transport by tests/cpp/shm_io_test.cpp (driven from tests/test_cpp_shm.py), and
//   exercised end to end by tests/test_shm_hot_load.py (a real engine serving Q1 over /dev/shm).
//   ParquetTables owns the arrow::Table, so the mapping stays alive for the table's lifetime.
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
