#include "soak_api.hpp"

#include <cstdlib>
#include <cstring>

// Models the loader holding the resident Arrow input. It allocates once and
// keeps the buffer for the life of the process, so the input stays resident and
// is re-handed to each freshly forked builder via the copy-on-write fork (no
// re-ingest). The builder restart-on-change must not disturb it.

static void* g_input = nullptr;

static size_t env_mb(const char* name, size_t fallback) {
    const char* v = getenv(name);
    return (v && *v) ? static_cast<size_t>(strtoull(v, nullptr, 10)) : fallback;
}

static void load() {
    if (g_input != nullptr) {
        return;
    }
    size_t bytes = env_mb("SOAK_INPUT_MB", 8) << 20;
    g_input = malloc(bytes);
    if (g_input != nullptr) {
        memset(g_input, 0xCD, bytes);  // fault the pages in so they are resident
    }
}

static const LoaderApi g_api{load};

extern "C" __attribute__((visibility("default")))
const void* plugin_query() { return &g_api; }
