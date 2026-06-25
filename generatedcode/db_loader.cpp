// db_loader.cpp — BFF bridge for the OLAP builder_api interface.
//
// The compile/run tools use the OLAP pipeline's builder_api which calls
// build(ParquetTables*) and destroy_database(Database*). This file provides
// those entry points and pulls in the full BFF write + read implementation.
//
// FILE_VERSION: 1

#include "builder_api.hpp"

// Pull in the full BFF write and read implementations directly so they are
// available in libbuilder.so without needing separate linker entries.
#include "write_impl.cpp"
#include "read_impl.cpp"

#include <cstdlib>
#include <string>

// Determine BFF storage directory from environment (set by the test runner).
static std::string bff_loader_storage_dir() {
    const char* env = std::getenv("STORAGE_DIR");
    if (env && env[0] != '\0') return env;
    return "tmp/bff_store";
}

Database* build(ParquetTables* tables) {
    if (!tables) throw std::runtime_error("build: null ParquetTables");

    std::string bff_dir = bff_loader_storage_dir();

    BffWriteOptions wopts;
    wopts.overwrite             = true;
    wopts.write_page_stats      = true;
    wopts.write_row_group_stats = true;
    wopts.write_footer_checksum = true;

    // Write BFF files and close the write handle.
    BffDataset* write_ds = write_bff_from_parquet_tables(tables, bff_dir, wopts);
    if (write_ds) close_bff_dataset(write_ds);

    // Open for reading (loads footer into RAM once).
    BffOpenOptions ropts;
    ropts.cache_footer             = true;
    ropts.validate_footer_checksum = false;
    ropts.io_mode                  = BffIoMode::Buffered;

    return build_bff_query_database(bff_dir, ropts);
}

void destroy_database(Database* db) {
    destroy_bff_query_database(db);
}