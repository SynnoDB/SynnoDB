// Reproduce-first leak driver for exception-safe build() in the SSD engine template.
//
// build() allocates the Database, its BufferPool, and the pool's page frames. A bad_alloc
// anywhere mid-build - the out-of-memory-during-build case that ratcheted the SF50 run to
// 480 GB - must free everything already allocated: the Database, the BufferPool, and every
// page frame constructed so far. Nothing frees them today, because ~Database does not own its
// members and destroy_database only runs on the success path.
//
// This driver installs a throwing operator new[] that fails partway through the frame-pool
// allocation (exactly where a large build runs out of memory), runs the real build(), and
// relies on AddressSanitizer's exit-time leak check to prove nothing was leaked.
//
//   throw  -> build() throws mid-frame-allocation; must be leak-free.
//   ok     -> build() succeeds; destroy_database frees it; must be leak-free and no double-free.

#include <cstdio>
#include <cstdlib>
#include <new>
#include <string>

#include "db_loader.hpp"

namespace {
// When armed, operator new[] fails once the cumulative bytes it has served cross a budget,
// simulating the frame-pool allocation running out of memory partway through the loop.
bool g_armed = false;
long g_budget_bytes = 0;
long g_served_bytes = 0;
}  // namespace

void* operator new[](std::size_t n) {
    if (g_armed) {
        g_served_bytes += static_cast<long>(n);
        if (g_served_bytes > g_budget_bytes) {
            g_armed = false;  // trip exactly once, mid-loop
            throw std::bad_alloc();
        }
    }
    void* p = std::malloc(n ? n : 1);
    if (!p) throw std::bad_alloc();
    return p;
}

void operator delete[](void* p) noexcept { std::free(p); }
void operator delete[](void* p, std::size_t) noexcept { std::free(p); }

int main(int argc, char** argv) {
    if (argc < 3) {
        std::fprintf(stderr, "usage: %s <throw|ok> <storage_dir>\n", argv[0]);
        return 2;
    }
    const std::string mode = argv[1];
    ::setenv("STORAGE_DIR", argv[2], 1);
    ::setenv("BUFFER_POOL_MB", "128", 1);  // -> 64 frames of 2 MiB each

    if (mode == "throw") {
        // Let a handful of 2 MiB frames allocate, then fail the next one: mid-pool, before
        // build() returns, so both the Database and the partial frame pool are in flight.
        g_served_bytes = 0;
        g_budget_bytes = 8L * 1024 * 1024;  // ~4 frames in, then throw
        g_armed = true;
        try {
            Database* db = build(nullptr);  // tables is unused by the storage-image build
            destroy_database(db);
            std::fprintf(stderr, "build() did not throw as expected\n");
            return 1;
        } catch (const std::bad_alloc&) {
            g_armed = false;
            return 0;  // ASan runs its leak check at process exit
        }
    }
    if (mode == "ok") {
        Database* db = build(nullptr);
        destroy_database(db);
        return 0;
    }
    std::fprintf(stderr, "unknown mode: %s\n", mode.c_str());
    return 2;
}
