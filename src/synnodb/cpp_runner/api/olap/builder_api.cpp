#include "builder_api.hpp"

#include "../abi_guard.hpp"
#include "../../hotpatch/plugin_base.hpp"

// FILE_VERSION: 2

// C-ABI shim for the builder plugin. The generated db_loader.cpp implements the
// C++ build()/destroy_database(); ParquetTables and Database cross the boundary
// as opaque void* the host never dereferences.

namespace {

void* abi_build(void* parquet_tables) {
    return synnodb::abi::guarded_call("builder", [&]() -> void* {
        return build(static_cast<ParquetTables*>(parquet_tables));
    });
}

void abi_destroy(void* database) {
    synnodb::abi::guarded_call("builder destroy", [&]() -> void* {
        destroy_database(static_cast<Database*>(database));
        return nullptr;
    });
}

}  // namespace

static const SynnoBuilderApi BUILDER = {
    .build = &abi_build,
    .destroy = &abi_destroy,
    .last_error = &synnodb::abi::last_error,
};

extern "C" __attribute__((visibility("default")))
const void*
plugin_query() {
    return &BUILDER;
}
