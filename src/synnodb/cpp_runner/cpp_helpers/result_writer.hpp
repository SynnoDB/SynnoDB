#pragma once
// result_writer.hpp - write a query's exact Arrow result where the runtime reads it.
//
// The runtime (router/process_engine.py ProcessEngine) hands the engine SYNNODB_RESULT_DIR (a
// /dev/shm directory for the hot-load) and reads back <dir>/result_<req_id>.arrow zero-copy.
// The engine builds its result with column_egress.hpp (decimal128 from exact int128) and calls
// write_result here - so input and output both ride Arrow, exact and zero-copy.

#include <cstdlib>
#include <filesystem>
#include <memory>
#include <string>

#include <arrow/table.h>

#include "shm_arrow_writer.hpp"

namespace synnodb {

// The directory the runtime asked us to write results into, or "results" relative to the
// engine's working directory when run standalone (./db <parquet_dir>).
inline std::string result_dir() {
    const char* env = std::getenv("SYNNODB_RESULT_DIR");
    return (env && env[0] != '\0') ? std::string(env) : std::string("results");
}

// Write *table* as Arrow IPC to <result_dir>/result_<req_id>.arrow.
inline void write_result(const std::shared_ptr<arrow::Table>& table, const std::string& req_id) {
    const std::string dir = result_dir();
    std::filesystem::create_directories(dir);
    WriteArrowTableToShm(table, dir + "/result_" + req_id + ".arrow");
}

}  // namespace synnodb
