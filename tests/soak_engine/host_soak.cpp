#include "pipeline.hpp"
#include "soak_api.hpp"

#include <cstdlib>
#include <iostream>

// Synthetic db host: a loader -> builder -> query pipeline built straight on
// pipeline.hpp, mirroring db_olap.cpp's structure with trivial plugins. The
// driver (test_pipeline_restart_soak.py) speaks the framed control protocol
// over two pipes whose fds arrive via env.
//
// SOAK_RESTART selects whether the loader restarts the builder PROCESS on a
// libbuilder_soak.so build-id change (the fix) or lets it reload in place (the
// pre-fix behavior / negative control). Both modes run the exact same binary, so
// the test toggles one bool and compares memory behavior.

static int env_fd(const char* name) {
    const char* v = getenv(name);
    if (v == nullptr || *v == '\0') {
        std::cerr << "host_soak: missing " << name << "\n";
        _exit(2);
    }
    return atoi(v);
}

// Held like db_olap.cpp's g_database so the builder teardown has a target.
static void* g_db = nullptr;

int main() {
    int read_fd = env_fd("SOAK_READ_FD");
    int done_fd = env_fd("SOAK_DONE_FD");
    bool restart = getenv("SOAK_RESTART") != nullptr;

    auto loader = stage<RunPolicy::OnChange>("./libloader_soak.so",
        [](Plugin& plugin) {
            auto api = plugin.get<LoaderApi>();
            api.load();
            return 0;
        });
    loader.restart_child_on_change = restart;

    auto builder = stage<RunPolicy::OnChange>("./libbuilder_soak.so",
        [](Plugin& plugin, int) {
            auto api = plugin.get<BuilderApi>();
            g_db = api.build();
            return 0;
        },
        [](Plugin& plugin) {
            auto api = plugin.get<BuilderApi>();
            api.destroy(g_db);
            g_db = nullptr;
        });

    auto query = stage<RunPolicy::AlwaysReload>("./libquery_soak.so",
        [](Plugin& plugin, int, const RunBatch&) {
            auto api = plugin.get<QueryApi>();
            api.query();
            return 0;
        });

    auto pipeline = make_pipeline(loader, builder, query);
    pipeline.run(read_fd, done_fd, false);
    return 0;
}
