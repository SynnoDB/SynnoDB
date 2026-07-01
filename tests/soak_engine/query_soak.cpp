#include "soak_api.hpp"

// Terminal stage. Does nothing: the soak test only exercises the loader/builder
// memory lifecycle, but a real query stage is needed so the pipeline emits a
// done token per RUN that the driver waits on.

static void query() {}

static const QueryApi g_api{query};

extern "C" __attribute__((visibility("default")))
const void* plugin_query() { return &g_api; }
