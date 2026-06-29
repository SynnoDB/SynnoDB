#pragma once
// ============================================================================
// shm_arrow_writer.hpp — zero-copy-egress: write a result arrow::Table as Arrow
// IPC into a /dev/shm segment for the Python parent to read back zero-copy.
//
// This is the C++ engine-worker egress side of the Phase-3 shared-memory data
// plane. The generated query (run_q<id>) builds its exact, typed arrow::Table with
// cpp_helpers/column_egress.hpp (make_table over decimal/int/double/string/bool/date/
// timestamp columns, NULLs and exact types via arrow::compute::Cast) and hands it here;
// the Python router reads it via src/synnodb/router/shm_transport.py::read_table.
//
// STATUS: compiled & validated by tests/cpp/shm_io_test.cpp against the tested
//   Python reader (round-trips both directions). Not yet wired into the engine's
//   build source list — integrate alongside shm_arrow_loader.hpp.
//
// Contract: writes the Arrow IPC *file* format (matches pyarrow ipc.open_file /
//   shm_transport.read_table). The engine owns the result segment's bytes; the
//   Python parent owns its name/lifecycle (creates the path, unlinks after read).
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
