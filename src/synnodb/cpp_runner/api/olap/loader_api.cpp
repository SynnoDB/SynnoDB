#include "loader_api.hpp"

#include "../abi_guard.hpp"
#include "../../hotpatch/plugin_base.hpp"

// FILE_VERSION: 2

// C-ABI shim for the loader plugin. The generated parquet_reader.cpp implements
// the C++ load()/destroy_parquet_tables(); ParquetTables crosses the boundary as
// an opaque void* the host never dereferences.

namespace {

void* abi_load(const char* parquet_dir) {
    return synnodb::abi::guarded_call("loader", [&]() -> void* {
        return load(std::string(parquet_dir ? parquet_dir : ""));
    });
}

void abi_destroy(void* parquet_tables) {
    // Teardown runs on the reload path, where pipeline.hpp already tolerates a
    // throw; guarding here keeps it from crossing the boundary to get there.
    synnodb::abi::guarded_call("loader destroy", [&]() -> void* {
        destroy_parquet_tables(static_cast<ParquetTables*>(parquet_tables));
        return nullptr;
    });
}

}  // namespace

static const SynnoLoaderApi LOADER = {
    .load = &abi_load,
    .destroy = &abi_destroy,
    .last_error = &synnodb::abi::last_error,
};

extern "C" __attribute__((visibility("default")))
const void*
plugin_query() {
    return &LOADER;
}
