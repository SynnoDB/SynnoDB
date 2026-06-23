#pragma once

#include "ingest_types.hpp"

#include <cstdint>
#include <string>

// Ingest Parquet into the custom BFF layout. Parquet decoding may use Arrow
// internally, but BFF readers are exposed as metadata + byte buffers.
BffDataset* write_bff_from_parquet(
    std::string parquet_dir,
    std::string bff_dir,
    const BffWriteOptions& options);

BffDataset* open_bff_dataset(std::string bff_dir, const BffOpenOptions& options);
void close_bff_dataset(BffDataset* dataset);

// Footer metadata is the main pruning surface. Implementations should cache it
// when BffOpenOptions::cache_footer is true.
BffFooter* load_bff_footer(BffDataset* dataset, bool refresh_cache);
const BffFooter* cached_bff_footer(BffDataset* dataset);
BffFooterInfo describe_bff_footer(const BffFooter* footer);
void invalidate_bff_footer_cache(BffDataset* dataset);

BffTable* open_bff_table(BffDataset* dataset, std::string table_name);
void close_bff_table(BffTable* table);

BffTableInfo describe_bff_table(const BffTable* table);
BffRowGroupInfo describe_bff_row_group(BffTable* table, std::uint32_t row_group_id);
BffPageInfo describe_bff_page(
    BffTable* table,
    std::uint32_t row_group_id,
    std::uint32_t column_id,
    std::uint32_t page_id);

// Granular reads return encoded or decoded buffers depending on read options.
// Direct I/O implementations should return buffers aligned to
// BffReadOptions::direct_io_alignment.
BffBuffer* read_bff_row_group(
    BffTable* table,
    std::uint32_t row_group_id,
    const BffColumnSelection& columns,
    const BffReadOptions& options);
BffBuffer* read_bff_page(
    BffTable* table,
    std::uint32_t row_group_id,
    std::uint32_t column_id,
    std::uint32_t page_id,
    const BffReadOptions& options);
void release_bff_buffer(BffBuffer* buffer);

struct IngestApi {
    BffDataset* (*write_from_parquet)(
        std::string,
        std::string,
        const BffWriteOptions&);
    BffDataset* (*open_dataset)(std::string, const BffOpenOptions&);
    void (*close_dataset)(BffDataset*);

    BffFooter* (*load_footer)(BffDataset*, bool);
    const BffFooter* (*cached_footer)(BffDataset*);
    BffFooterInfo (*describe_footer)(const BffFooter*);
    void (*invalidate_footer_cache)(BffDataset*);

    BffTable* (*open_table)(BffDataset*, std::string);
    void (*close_table)(BffTable*);
    BffTableInfo (*describe_table)(const BffTable*);
    BffRowGroupInfo (*describe_row_group)(BffTable*, std::uint32_t);
    BffPageInfo (*describe_page)(
        BffTable*,
        std::uint32_t,
        std::uint32_t,
        std::uint32_t);

    BffBuffer* (*read_row_group)(
        BffTable*,
        std::uint32_t,
        const BffColumnSelection&,
        const BffReadOptions&);
    BffBuffer* (*read_page)(
        BffTable*,
        std::uint32_t,
        std::uint32_t,
        std::uint32_t,
        const BffReadOptions&);
    void (*release_buffer)(BffBuffer*);
};
