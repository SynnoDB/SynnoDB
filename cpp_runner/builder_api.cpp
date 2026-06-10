#include "builder_api.hpp"

#include "utils/plugin_base.hpp"

// FILE_VERSION: 1

static const BuilderApi BUILDER = {
    .build = &build,
    .destroy = &destroy_database,
};

extern "C" __attribute__((visibility("default")))
const void*
plugin_query() {
    return &BUILDER;
}
