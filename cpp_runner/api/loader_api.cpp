#include "loader_api.hpp"

#include "../hotpatch/plugin_base.hpp"


// FILE_VERSION: 1

static const LoaderApi LOADER = {
    .load = &load,
    .destroy = &destroy_parquet_tables,
};

extern "C" __attribute__((visibility("default")))
const void*
plugin_query() {
    return &LOADER;
}
