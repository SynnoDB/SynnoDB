#include "../utils/plugin_base.hpp"
#include "iface_ingest.hpp"

#include <cstdio>
#include <unistd.h>

static int ingest_(int v) {
    fprintf(stderr, "ingest start\n");
    sleep(1);
    fprintf(stderr, "ingest done\n");
    return v + 3;
}

static const IngestApi INGEST = {
    .ingest = &ingest_,
};

extern "C" __attribute__((visibility("default")))
const void*
plugin_query() {
    return &INGEST;
}
