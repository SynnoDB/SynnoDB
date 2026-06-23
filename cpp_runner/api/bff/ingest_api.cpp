#include "ingest_api.hpp"

#include "../hotpatch/plugin_base.hpp"

static const IngestApi INGEST = {
    .write_from_parquet = &write_bff_from_parquet,
    .open_dataset = &open_bff_dataset,
    .close_dataset = &close_bff_dataset,
    .load_footer = &load_bff_footer,
    .cached_footer = &cached_bff_footer,
    .describe_footer = &describe_bff_footer,
    .invalidate_footer_cache = &invalidate_bff_footer_cache,
    .open_table = &open_bff_table,
    .close_table = &close_bff_table,
    .describe_table = &describe_bff_table,
    .describe_row_group = &describe_bff_row_group,
    .describe_page = &describe_bff_page,
    .read_row_group = &read_bff_row_group,
    .read_page = &read_bff_page,
    .release_buffer = &release_bff_buffer,
};

extern "C" __attribute__((visibility("default")))
const void*
plugin_query() {
    return &INGEST;
}
