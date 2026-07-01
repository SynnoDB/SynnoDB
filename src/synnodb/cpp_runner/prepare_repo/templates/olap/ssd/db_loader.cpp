#include "db_loader.hpp"
#include "file_loader_utils.hpp"

#include <cstdlib>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <stdexcept>
#include <string>


// Return the storage directory for column files. STORAGE_DIR is set by the
// Python runner per scale-factor for persistent-storage modes; an unset or
// empty value indicates a misconfiguration and must not silently fall back to
// a workspace-local path (which the framework would not clean between runs).
static std::string get_storage_dir() {
    const char* env = std::getenv("STORAGE_DIR");
    if (!env || env[0] == '\0') {
        throw std::runtime_error("STORAGE_DIR env var is not set");
    }
    return std::string(env);
}

// Buffer-pool frame budget (MiB). The parent runner has already applied the
// frame-pool / mmap-headroom split before exporting BUFFER_POOL_MB, so this
// value is consumed as-is. RLIMIT_AS is set separately by the runner to bound
// frame allocations + mmap_col regions together.
static int64_t get_buffer_pool_frames() {
    const char* env = std::getenv("BUFFER_POOL_MB");
    int64_t pool_mb = env ? std::stoll(env) : 1024LL;
    int64_t frames = (pool_mb * 1024LL * 1024LL) / BP_PAGE_BYTES;
    return frames < 64 ? 64 : frames;
}


Database* build(ParquetTables* tables) {
    // TODO: Implement SSD-backed column serialization in three steps:
    //
    // Step 1 - Choose a frame budget and create the shared BufferPool.
    //   get_buffer_pool_frames() reads BUFFER_POOL_MB (the frame-pool share of
    //   the total RAM budget, already pre-split by the parent runner) and
    //   converts it to a frame count. Defaults to 1 GiB with >= 64 frames.
    //
    // Step 2 - For each needed column in each table path in `tables`:
    //   a) Open the Parquet file with ParquetFileScanner and read only the
    //      needed column(s), one row group at a time. Do NOT materialize all
    //      tables in RAM.
    //   b) Use the helpers in file_loader_utils.hpp to write flat column files:
    //        FdStream f;
    //        f.open(storage_dir + "lineitem.l_orderkey.bin");
    //        write_col_int32(f, table->column(0).get());
    //
    //      Store dates as int32_t. Store decimals as scaled int64_t where
    //      possible. Store strings as two files:
    //        uint64_t offsets[num_rows + 1] and char bytes[total_bytes].
    //   c) Register the file with the pool and store the handle with
    //      reg_int32(), reg_int64(), reg_fixed_width<T>(), or reg_string().
    //
    // Step 3 - Release each Arrow row-group/table batch before moving to the
    //   next one, then return the populated Database*.
    //
    // Note: build() runs when the hotpatch builder stage reruns. The framework
    //   clears STORAGE_DIR immediately before calling build(), so this function
    //   should write a fresh storage image rather than managing cleanup itself.

    const std::string storage_dir = get_storage_dir();
    std::filesystem::create_directories(storage_dir);

    // Own the partial dataset while it is being built: if any step below throws (a large build
    // running out of memory is the expected case), unwinding destroys db, which frees the pool
    // and every column handle. Ownership is handed to the caller only once build() succeeds.
    auto db = std::make_unique<Database>();
    db->pool = std::make_unique<BufferPool>(get_buffer_pool_frames());

    // TODO: serialize columns and populate db handles here, e.g.
    //   db->l_orderkey = reg_int32(db->pool.get(), storage_dir + "lineitem.l_orderkey.bin", n);

    return db.release();
}

void destroy_database(Database* db) {
    // ~Database frees the BufferPool and every column handle it owns.
    delete db;
}
