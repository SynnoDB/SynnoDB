#include "../utils/plugin_base.hpp"
#include "iface_process.hpp"

#include <cstdio>
#include <stdexcept>
#include <unistd.h>

static int process_(int v) {
    fprintf(stderr, "process start\n");
    sleep(1);
    fprintf(stderr, "process done\n");
    // fprintf(stderr, "segfaulting now\n");
    //*reinterpret_cast<uint64_t*>(0x0) = 42;
    // fprintf(stderr, "segfault happened\n");
    // throw std::runtime_error("exception in process");
    return v * 5;
}

static const ProcessApi PROCESS = {
    .process = &process_,
};

extern "C" __attribute__((visibility("default")))
const void*
plugin_query() {
    return &PROCESS;
}
