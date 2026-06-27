// OLAP use-case: loader + builder ingest stages wrapping the shared query stage.

#include "db_usecase.hpp"
#include "builder_api.hpp"
#include "loader_api.hpp"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>

// FILE_VERSION: 1

Database* g_database = nullptr;

struct OlapState {
    std::string parquet_path;
    ParquetTables* parquet_tables = nullptr;
};

static OlapState olap_state;

static void clear_storage_dir_if_configured() {
    const char* env = std::getenv("STORAGE_DIR");
    if (!env || env[0] == '\0') {
        return;
    }

    std::filesystem::path storage_dir(env);

    // Refuse to delete a directory we didn't create: run.py drops a
    // .bespoke_storage_dir marker into every storage dir it sets up, so a
    // misconfigured STORAGE_DIR pointing at unrelated data is rejected here.
    if (!std::filesystem::exists(storage_dir / ".bespoke_storage_dir")) {
        throw std::runtime_error(
            "Refusing to clear STORAGE_DIR " + storage_dir.string() +
            ": missing .bespoke_storage_dir sentinel");
    }

    std::error_code ec;
    std::filesystem::remove_all(storage_dir, ec);
    if (ec) {
        throw std::runtime_error(
            "Failed to remove STORAGE_DIR " + storage_dir.string() + ": " + ec.message());
    }

    std::filesystem::create_directories(storage_dir, ec);
    if (ec) {
        throw std::runtime_error(
            "Failed to create STORAGE_DIR " + storage_dir.string() + ": " + ec.message());
    }

    // Re-create the sentinel file after clearing the directory.
    std::ofstream(storage_dir / ".bespoke_storage_dir").close();
}

bool usecase_parse_args(int argc, char** argv) {
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <PARQUET_DIR>\n";
        return false;
    }
    olap_state.parquet_path = argv[1];
    return true;
}

void usecase_run_child(int read_fd, int done_fd) {
    auto pipeline = make_pipeline(
        // ── Loader stage ────────────────────────────────────────────────────
        // Behaviour differs by storage mode (controlled at code-generation time):
        //
        //   In-memory mode:  api.load() reads all Parquet files via Arrow and
        //     materialises them as Arrow tables in RAM.  The builder stage then
        //     converts those tables into the in-memory Database struct.
        //
        //   Persistent-storage (SSD) mode:  api.load() is a trivial no-op that
        //     only records the per-scale-factor Parquet directory as file-path
        //     strings inside ParquetTables (e.g. tables->lineitem_path).  No
        //     Arrow data is read here.  The builder stage later opens those
        //     paths itself, streams columns row-group by row-group, and writes
        //     them to binary column files on disk.
        //
        // The stage is kept in both modes so that the builder always receives a
        // populated ParquetTables* with the correct file paths, without needing
        // to know the parquet directory itself.
        stage<RunPolicy::OnChange>("./build/libloader.so",
            [](Plugin& plugin) {
                auto api = plugin.get<LoaderApi>();
                std::cerr << "loader start\n";
                olap_state.parquet_tables = api.load(olap_state.parquet_path);
                std::cerr << "loader done\n";
                return 0;
            },
            [](Plugin& plugin) {
                // Destroy old tables with the old plugin BEFORE dlclose so that
                // shared_ptr deleters and Arrow statics in libloader.so are still
                // mapped when the destructor chain runs.
                auto api = plugin.get<LoaderApi>();
                if (olap_state.parquet_tables) {
                    api.destroy(olap_state.parquet_tables);
                    olap_state.parquet_tables = nullptr;
                }
            }),
        // ── Builder stage ────────────────────────────────────────────────────
        // Behaviour differs by storage mode:
        //
        //   In-memory mode:  api.build() converts the Arrow tables produced by
        //     the loader into an optimised in-memory Database struct (column
        //     vectors, CSR indexes, pre-joined columns, etc.).  All data lives
        //     in RAM for the lifetime of the process.
        //
        //   Persistent-storage (SSD) mode:  api.build() opens the Parquet files
        //     via the paths in ParquetTables, serialises each column to a flat
        //     binary file under STORAGE_DIR (set by run.py per scale-factor),
        //     and returns a Database whose fields
        //     are ColumnHandle<T> descriptors backed by a shared BufferPool.
        //     Column pages are loaded from SSD on demand at query time.
        //     The runner clears STORAGE_DIR exactly when this builder stage
        //     reruns, so query-only hotpatches keep the existing files while
        //     storage-layout or loader/builder reloads rebuild them from scratch.
        stage<RunPolicy::OnChange>("./build/libbuilder.so",
            [](Plugin& plugin, int) {
                auto api = plugin.get<BuilderApi>();
                clear_storage_dir_if_configured();
                std::cerr << "builder start\n";
                const auto t0 = std::chrono::steady_clock::now();
                g_database = api.build(olap_state.parquet_tables);
                std::cerr << "builder done\n";
                const auto t1 = std::chrono::steady_clock::now();
                const float ms =
                    std::chrono::duration<float, std::milli>(t1 - t0).count();
                std::cerr << "Ingest ms: " << ms << "\n";
                return 0;
            },
            [](Plugin& plugin) {
                auto api = plugin.get<BuilderApi>();
                if (g_database) {
                    api.destroy(g_database);
                    g_database = nullptr;
                }
            }),
        make_query_stage()
    );
    pipeline.run(read_fd, done_fd, false);
}
