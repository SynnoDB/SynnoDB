#pragma once
// ============================================================================
// shm_paths.hpp - resolve a table name to its /dev/shm Arrow-IPC ingest segment.
//
// The shm hot-load (the Python side is src/synnodb/router/process_engine.py
// ShmHotLoadEngine) writes each table as an Arrow IPC file to
// <SYNNODB_SHM_INGEST>/<table>.arrow and passes that directory in the
// SYNNODB_SHM_INGEST environment variable (read here exactly like STORAGE_DIR).
// The generated loader's load() consults shm_ingest_enabled(): when set it maps
// each table zero-copy via ReadArrowTableFromShm(shm_ingest_path_for(table));
// otherwise it reads parquet as before. The two planes share one binary.
// ============================================================================

#include <cstdlib>
#include <string>

namespace synnodb {

// The ingest directory from SYNNODB_SHM_INGEST, or "" when the shm plane is off.
inline std::string shm_ingest_dir() {
    const char* env = std::getenv("SYNNODB_SHM_INGEST");
    return (env && env[0] != '\0') ? std::string(env) : std::string();
}

inline bool shm_ingest_enabled() { return !shm_ingest_dir().empty(); }

// <dir>/<table>.arrow - the segment the Python parent wrote for this table.
inline std::string shm_ingest_path_for(const std::string& table) {
    std::string dir = shm_ingest_dir();
    if (!dir.empty() && dir.back() != '/') dir.push_back('/');
    return dir + table + ".arrow";
}

}  // namespace synnodb
