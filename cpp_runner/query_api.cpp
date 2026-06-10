#include "query_api.hpp"

#include "utils/plugin_base.hpp"

// FILE_VERSION: 1

static const QueryApi QUERY = {
    .query = &query,
};

extern "C" __attribute__((visibility("default")))
const void*
plugin_query() {
    return &QUERY;
}
