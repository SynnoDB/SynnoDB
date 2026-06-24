#include "read_api.hpp"
#include "bff_format.hpp"

#include <utility>

// FILE_VERSION: 1
//
// Skeleton implementation of the BFF read API (read_api.hpp + the plan_bff_scan
// hook from filter_pushdown.hpp). The agent should replace these stubs with a
// real reader that:
//   * opens the .bff table files under the dataset directory,
//   * loads and (optionally) caches the footer,
//   * prunes row groups / pages / columns from footer stats,
//   * returns encoded or decoded byte buffers for the selected ranges.
//
// The stubs compile and run so the pipeline is exercised end to end; queries
// that rely on real data see empty results until this is implemented. This file
// is linked into BOTH libwriter.so (via the IngestApi) and libquery.so (so the
// generated query code can call the read API directly).

BffDataset* open_bff_dataset(std::string bff_dir, const BffOpenOptions& options) {
    auto* dataset = new BffDataset();
    dataset->root_path = std::move(bff_dir);
    dataset->options = options;
    return dataset;
}

void close_bff_dataset(BffDataset* dataset) {
    delete dataset;
}

// Framework plumbing (you do not need to edit this): build/tear down the
// query-time Database handle. The host calls build_bff_query_database() once in
// the writer stage so the dataset is opened and its footer decoded a single
// time; the result is cached in g_database and threaded into every run_q<N>().
// This composes the open + load_footer you implement above, so it automatically
// reflects your footer format.
Database* build_bff_query_database(std::string bff_dir, const BffOpenOptions& options) {
    auto* db = new Database();
    db->dataset = open_bff_dataset(std::move(bff_dir), options);
    db->footer = load_bff_footer(db->dataset, /*refresh_cache=*/false);
    return db;
}

void destroy_bff_query_database(Database* db) {
    if (!db) {
        return;
    }
    close_bff_dataset(db->dataset);
    delete db;
}

BffFooter* load_bff_footer(BffDataset* dataset, bool /*refresh_cache*/) {
    if (!dataset) {
        return nullptr;
    }
    // TODO: read the trailer + footer bytes from disk and decode them into
    // dataset->footer.info. For now we expose an empty (but valid) footer.
    dataset->footer.info.root_path = dataset->root_path;
    dataset->has_footer = true;
    return &dataset->footer;
}

const BffFooter* cached_bff_footer(BffDataset* dataset) {
    if (!dataset || !dataset->has_footer) {
        return nullptr;
    }
    return &dataset->footer;
}

BffFooterInfo describe_bff_footer(const BffFooter* footer) {
    if (!footer) {
        return {};
    }
    return footer->info;
}

void invalidate_bff_footer_cache(BffDataset* dataset) {
    if (dataset) {
        dataset->has_footer = false;
    }
}

BffTable* open_bff_table(BffDataset* dataset, std::string table_name) {
    auto* table = new BffTable();
    table->dataset = dataset;
    table->info.name = std::move(table_name);
    if (dataset) {
        table->info.path = dataset->root_path;
    }
    return table;
}

void close_bff_table(BffTable* table) {
    delete table;
}

BffTableInfo describe_bff_table(const BffTable* table) {
    if (!table) {
        return {};
    }
    return table->info;
}

BffRowGroupInfo describe_bff_row_group(BffTable* table, std::uint32_t row_group_id) {
    if (table && row_group_id < table->info.row_groups.size()) {
        return table->info.row_groups[row_group_id];
    }
    return {};
}

BffPageInfo describe_bff_page(
    BffTable* /*table*/,
    std::uint32_t row_group_id,
    std::uint32_t column_id,
    std::uint32_t page_id) {
    BffPageInfo info;
    info.row_group_id = row_group_id;
    info.column_id = column_id;
    info.page_id = page_id;
    return info;
}

BffScanPlan plan_bff_scan(BffTable* /*table*/, const BffScanRequest& /*request*/) {
    // TODO: use footer/page stats to build a metadata-only read plan.
    return {};
}

BffBuffer* read_bff_row_group(
    BffTable* /*table*/,
    std::uint32_t /*row_group_id*/,
    const BffColumnSelection& /*columns*/,
    const BffReadOptions& /*options*/) {
    // TODO: read the selected columns of the row group into a buffer.
    return new BffBuffer();
}

BffBuffer* read_bff_page(
    BffTable* /*table*/,
    std::uint32_t /*row_group_id*/,
    std::uint32_t /*column_id*/,
    std::uint32_t /*page_id*/,
    const BffReadOptions& /*options*/) {
    // TODO: read a single page into a buffer.
    return new BffBuffer();
}

void release_bff_buffer(BffBuffer* buffer) {
    delete buffer;
}
