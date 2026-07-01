#pragma once

// Minimal stage APIs for the restart-on-change soak engine. Each plugin .so
// exports plugin_query() returning a pointer to one of these structs; the host
// (host_soak.cpp) calls through it, mirroring the real loader/builder/query
// contract without pulling in the full Database/Arrow machinery.

struct LoaderApi {
    void (*load)();
};

struct BuilderApi {
    void* (*build)();
    void (*destroy)(void*);
};

struct QueryApi {
    void (*query)();
};
