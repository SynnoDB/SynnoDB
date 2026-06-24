#include "ingest_api.hpp"

#include "../hotpatch/plugin_base.hpp"

static const IngestApi INGEST = {
    .write = {
        .write_from_parquet = &write_bff_from_parquet,
        .write_from_parquet_tables = &write_bff_from_parquet_tables,
    },
    .read = {
        .open_dataset = &open_bff_dataset,
        .close_dataset = &close_bff_dataset,
        .build_query_database = &build_bff_query_database,
        .destroy_query_database = &destroy_bff_query_database,
        .load_footer = &load_bff_footer,
        .cached_footer = &cached_bff_footer,
        .describe_footer = &describe_bff_footer,
        .invalidate_footer_cache = &invalidate_bff_footer_cache,
        .open_table = &open_bff_table,
        .close_table = &close_bff_table,
        .describe_table = &describe_bff_table,
        .describe_row_group = &describe_bff_row_group,
        .describe_page = &describe_bff_page,
        .plan_scan = &plan_bff_scan,
        .read_row_group = &read_bff_row_group,
        .read_page = &read_bff_page,
        .release_buffer = &release_bff_buffer,
    },
    .binding = {
        .plan_scan = &plan_bff_binding_scan,
        .read_batch = &read_bff_binding_batch,
        .release_batch = &release_bff_binding_batch,
    },
};

extern "C" __attribute__((visibility("default")))
const void*
plugin_query() {
    return &INGEST;
}
