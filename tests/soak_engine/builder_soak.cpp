#include "soak_api.hpp"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <unistd.h>

// Models the builder's materialized dataset. The defect this engine reproduces
// is that an in-place teardown does NOT return the dataset's pages to the OS
// (the real cause is glibc retaining freed arenas after destroy_database). Here
// each build() leaves one dataset-sized block resident for the life of THIS
// process and destroy() returns nothing. A long-lived builder that rebuilds in
// place therefore ratchets RSS by one copy per source-change reload; a builder
// restarted as a fresh process per source change caps resident memory at a
// single copy - exactly the invariant the soak test pins.
//
// The block is a single large allocation (mmap-backed), so it is unambiguously
// resident once faulted and free of glibc arena-coalescing nondeterminism.

static size_t env_mb(const char* name, size_t fallback) {
    const char* v = getenv(name);
    return (v && *v) ? static_cast<size_t>(strtoull(v, nullptr, 10)) : fallback;
}

static void* build() {
    // Report this builder's PID so the test can observe whether the process
    // identity changes across source-change reloads (it must, with the fix).
    if (const char* pid_file = getenv("SOAK_PID_FILE")) {
        FILE* f = fopen(pid_file, "w");
        if (f != nullptr) {
            fprintf(f, "%d", static_cast<int>(getpid()));
            fclose(f);
        }
    }
    size_t bytes = env_mb("SOAK_HOG_MB", 32) << 20;
    void* p = malloc(bytes);
    if (p != nullptr) {
        memset(p, 0xAB, bytes);  // fault the pages in so they are resident
    }
    return p;  // intentionally leaked: models memory not returned to the OS
}

static void destroy(void*) {
    // No-op: the defect under test is that in-place teardown does not return the
    // dataset's pages to the OS. Only process exit reclaims them.
}

static const BuilderApi g_api{build, destroy};

extern "C" __attribute__((visibility("default")))
const void* plugin_query() { return &g_api; }
