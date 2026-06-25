#include "write_api.hpp"
#include "bff_format.hpp"
#include "parquet_reader.hpp"

#include <filesystem>
#include <utility>

// FILE_VERSION: 2
//
// Skeleton implementation of the BFF write API (the file-format encoder). The
// agent should replace these stubs with a real writer that serialises the Arrow
// tables into the bespoke .bff layout described in api/bff/README.md:
//
//   file header / row group pages / footer / trailer
//
// per-column encodings, page/row-group stats for pruning, dictionaries, etc.
// The stubs only create the output directory so the writer stage of the
// pipeline succeeds and the query stage has a dataset directory to open.

namespace {

// Create the dataset directory and return an open dataset handle. The handle is
// later closed by the read API's close_bff_dataset (see db_bff.cpp teardown).
BffDataset* make_empty_dataset(std::string bff_dir) {
    std::error_code ec;
    std::filesystem::create_directories(bff_dir, ec);

    auto* dataset = new BffDataset();
    dataset->root_path = bff_dir;
    dataset->has_footer = true;
    dataset->footer.info.root_path = std::move(bff_dir);
    return dataset;
}

} // namespace

BffDataset* write_bff_from_parquet(
    std::string /*parquet_dir*/,
    std::string bff_dir,
    const BffWriteOptions& /*options*/) {
    // TODO: decode the Parquet files (Arrow internally) and encode them into the
    // BFF layout under bff_dir.
    return make_empty_dataset(std::move(bff_dir));
}

BffDataset* write_bff_from_parquet_tables(
    const ParquetTables* /*tables*/,
    std::string bff_dir,
    const BffWriteOptions& /*options*/) {
    // TODO: iterate over the ArrowTable fields declared in parquet_reader.hpp
    // and encode each one into a .bff table file under bff_dir, recording footer
    // metadata (schema, row groups, pages, stats) as you go.
    return make_empty_dataset(std::move(bff_dir));
}
