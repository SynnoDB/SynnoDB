// BFF use-case: loader + writer ingest stages wrapping the shared query stage.
//
// Three hot-patchable stages, mirroring db_olap.cpp but targeting the bespoke
// BFF on-disk file format instead of an in-memory / SSD column store:
//
//   1. Loader  — read Parquet via Arrow into in-memory ParquetTables.
//   2. Writer  — serialise those tables into the BFF layout (.bff files) under
//                STORAGE_DIR, i.e. the bespoke file format itself.
//   3. Query   — read back from the BFF file format to answer queries.
//
// Each stage is a separately hot-reloadable .so so the LLM can iterate on the
// writer (file-format encoder) and the query/reader independently without
// re-running the (expensive) Parquet load.

#include "db_usecase.hpp"
#include "ingest_api.hpp"
#include "loader_api.hpp"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>

// FILE_VERSION: 1

Database* g_database = nullptr;

namespace {

struct BffState {
    std::string parquet_path;
    std::string bff_dir;
    ParquetTables* parquet_tables = nullptr;
    // Write-side dataset handle returned by the writer plugin. Owned by
    // libwriter.so (the IngestApi plugin); must be closed with that same plugin
    // before it is dlclose'd — see the writer teardown below.
    BffDataset* dataset = nullptr;
};

BffState bff_state;

// Directory the BFF dataset is written to and later read from. run.py points
// STORAGE_DIR at a per-scale-factor directory and drops a .bespoke_storage_dir
// marker into it (see workload_provider_bff.py). Unlike OLAP in-memory mode the
// BFF format always lives on disk, so STORAGE_DIR is mandatory here.
std::string bff_storage_dir() {
    const char* env = std::getenv("STORAGE_DIR");
    if (!env || env[0] == '\0') {
        throw std::runtime_error(
            "STORAGE_DIR not set: the BFF use-case needs a dataset directory");
    }
    return env;
}

// Wipe and re-create the BFF dataset directory before the writer runs.
//
// Refuse to delete a directory we didn't create: run.py drops a
// .bespoke_storage_dir marker into every storage dir it sets up, so a
// misconfigured STORAGE_DIR pointing at unrelated data is rejected here.
//
// This runs exactly when the writer stage (re)runs, so query-only hotpatches
// keep the existing .bff files while loader/writer reloads rebuild them from
// scratch.
void clear_bff_storage_dir() {
    std::filesystem::path storage_dir(bff_storage_dir());

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

} // namespace

bool usecase_parse_args(int argc, char** argv) {
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <PARQUET_DIR>\n";
        return false;
    }
    bff_state.parquet_path = argv[1];
    return true;
}

void usecase_run_child(int read_fd, int done_fd) {
    auto pipeline = make_pipeline(
        // ── Loader stage ────────────────────────────────────────────────────
        // api.load() reads every Parquet file in the dataset directory via Arrow
        // and materialises them as Arrow tables inside ParquetTables. This is the
        // same loader used by the OLAP use-case; the BFF writer consumes the
        // resulting ParquetTables rather than re-reading Parquet itself.
        stage<RunPolicy::OnChange>("./build/libloader.so",
            [](Plugin& plugin) {
                auto api = plugin.get<LoaderApi>();
                std::cerr << "loader start\n";
                bff_state.parquet_tables = api.load(bff_state.parquet_path);
                std::cerr << "loader done\n";
                return 0;
            },
            [](Plugin& plugin) {
                // Destroy old tables with the old plugin BEFORE dlclose so that
                // shared_ptr deleters and Arrow statics in libloader.so are still
                // mapped when the destructor chain runs.
                auto api = plugin.get<LoaderApi>();
                if (bff_state.parquet_tables) {
                    api.destroy(bff_state.parquet_tables);
                    bff_state.parquet_tables = nullptr;
                }
            }),
        // ── Writer stage ─────────────────────────────────────────────────────
        // api.write.write_from_parquet_tables() encodes the Arrow tables into the
        // bespoke BFF layout (file header / row groups / pages / footer / trailer)
        // and writes one .bff file per table under STORAGE_DIR. Parquet/Arrow may
        // be used internally here, but the resulting dataset is read back at query
        // time through the native BFF metadata + byte-buffer API, not via Arrow.
        //
        // The directory is cleared first so a re-run never mixes pages from an
        // older format revision with freshly written ones.
        stage<RunPolicy::OnChange>("./build/libwriter.so",
            [](Plugin& plugin, int) {
                auto api = plugin.get<IngestApi>();
                clear_bff_storage_dir();
                bff_state.bff_dir = bff_storage_dir();

                BffWriteOptions options;
                options.overwrite = true;

                std::cerr << "bff writer start\n";
                const auto t0 = std::chrono::steady_clock::now();
                bff_state.dataset = api.write.write_from_parquet_tables(
                    bff_state.parquet_tables, bff_state.bff_dir, options);
                std::cerr << "bff writer done\n";
                const auto t1 = std::chrono::steady_clock::now();
                const float ms =
                    std::chrono::duration<float, std::milli>(t1 - t0).count();
                // Python extracts ingest time via the "Ingest ms:" prefix.
                std::cerr << "Ingest ms: " << ms << "\n";

                // Open the just-written dataset and decode its footer ONCE, here
                // in the (OnChange) writer stage, and publish it as g_database.
                // Because g_database lives in the host it survives query-plugin
                // hot-reloads, so each run_q<N>() receives an already-opened,
                // footer-loaded handle as `db` instead of re-opening the dataset
                // and re-parsing the footer on every query. This is the BFF
                // analogue of the OLAP builder stage's in-memory Database. Footer
                // open time is intentionally excluded from "Ingest ms" (a write
                // metric); it is a one-time query-setup cost, like the OLAP build.
                BffOpenOptions open_options;
                g_database = api.read.build_query_database(
                    bff_state.bff_dir, open_options);
                return 0;
            },
            [](Plugin& plugin) {
                // Tear down read-side handles with the plugin that allocated them,
                // before it is unmapped on reload. The .bff files are already
                // flushed to disk. g_database is destroyed here (not in the query
                // stage) so it persists across query-only hotpatches and is only
                // rebuilt when the writer/format itself changes.
                auto api = plugin.get<IngestApi>();
                if (g_database) {
                    api.read.destroy_query_database(g_database);
                    g_database = nullptr;
                }
                if (bff_state.dataset) {
                    api.read.close_dataset(bff_state.dataset);
                    bff_state.dataset = nullptr;
                }
            }),
        // ── Query stage ──────────────────────────────────────────────────────
        // The shared query stage reads back from the BFF file format: each
        // run_q<N>() receives g_database (the open .bff dataset + decoded footer
        // built in the writer stage), prunes row groups/pages from the footer,
        // and reads the byte buffers it needs via the BFF read API. g_database is
        // built once in the writer stage and survives query-only hotpatches.
        make_query_stage()
    );
    pipeline.run(read_fd, done_fd, false);
}
