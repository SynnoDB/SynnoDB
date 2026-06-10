#include "../utils/plugin_base.hpp"
#include "iface_query.hpp"

#include <cstdio>
#include <unistd.h>

static int query_(int v) {
    fprintf(stderr, "query start\n");
    sleep(1);
    fprintf(stderr, "query done\n");
    return v - 3;
}

static const QueryApi QUERY = {
    .query = &query_,
};

extern "C" __attribute__((visibility("default")))
const void*
plugin_query() {
    return &QUERY;
}
