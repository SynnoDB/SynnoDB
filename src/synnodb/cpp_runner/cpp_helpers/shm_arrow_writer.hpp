#pragma once
// ============================================================================
// shm_arrow_writer.hpp — zero-copy-egress: write a result arrow::Table as Arrow
// IPC into a /dev/shm segment for the Python parent to read back zero-copy.
//
// The C++ engine egress side of the shared-memory data plane. The generated query
// (run_q<id>) builds its exact, typed arrow::Table with cpp_helpers/column_egress.hpp
// (make_table over decimal/int/double/string/bool/date/timestamp columns, NULLs and exact
// types via arrow::compute::Cast) and hands it to cpp_helpers/result_writer.hpp, which calls
// WriteArrowTableToShm to emit result_<req_id>.arrow under SYNNODB_RESULT_DIR (ShmHotLoadEngine
// points that at the /dev/shm ingest dir, so the result rides shared memory too).
//
// Compiled into every engine via result_writer.hpp, and validated both directions against the
// Python reader by tests/cpp/shm_io_test.cpp (driven from tests/test_cpp_shm.py). The Python
// router reads the result via router/process_engine._read_arrow.
//
// Contract: writes the Arrow IPC *file* format (matches pyarrow ipc.open_file /
//   shm_transport.read_table). The engine owns the result segment's bytes; the Python parent
//   owns its name/lifecycle (creates the dir, unlinks after read).
// ============================================================================

#include <memory>
#include <stdexcept>
#include <string>

#include <arrow/io/file.h>
#include <arrow/ipc/writer.h>
#include <arrow/table.h>

namespace synnodb {

// Serialize *table* as an Arrow IPC file into *shm_path*. Throws on any failure
// (the Python router treats an engine exception as a fallback to DuckDB).
inline void WriteArrowTableToShm(const std::shared_ptr<arrow::Table>& table,
                                 const std::string& shm_path) {
    auto sink_result = arrow::io::FileOutputStream::Open(shm_path);
    if (!sink_result.ok()) {
        throw std::runtime_error("shm create failed for " + shm_path + ": " +
                                 sink_result.status().ToString());
    }
    std::shared_ptr<arrow::io::FileOutputStream> sink = sink_result.ValueOrDie();

    auto writer_result = arrow::ipc::MakeFileWriter(sink, table->schema());
    if (!writer_result.ok()) {
        throw std::runtime_error("arrow ipc writer failed for " + shm_path + ": " +
                                 writer_result.status().ToString());
    }
    std::shared_ptr<arrow::ipc::RecordBatchWriter> writer = writer_result.ValueOrDie();

    arrow::Status st = writer->WriteTable(*table);
    if (!st.ok()) {
        throw std::runtime_error("arrow ipc WriteTable failed for " + shm_path + ": " + st.ToString());
    }
    st = writer->Close();
    if (!st.ok()) {
        throw std::runtime_error("arrow ipc writer Close failed for " + shm_path + ": " + st.ToString());
    }
    st = sink->Close();
    if (!st.ok()) {
        throw std::runtime_error("shm sink Close failed for " + shm_path + ": " + st.ToString());
    }
}

}  // namespace synnodb
