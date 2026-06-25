#pragma once

#include "ingest_types.hpp"

#include <string>

struct ParquetTables;

// Ingest Parquet into the custom BFF layout. Parquet decoding may use Arrow
// internally, but BFF readers are exposed as metadata + byte buffers.
BffDataset* write_bff_from_parquet(
    std::string parquet_dir,
    std::string bff_dir,
    const BffWriteOptions& options);

// Ingest from the generated ParquetTables structure used by the OLAP templates.
// Implementations should include the generated parquet_reader.hpp to inspect
// its concrete table fields.
BffDataset* write_bff_from_parquet_tables(
    const ParquetTables* tables,
    std::string bff_dir,
    const BffWriteOptions& options);

struct BffWriteApi {
    BffDataset* (*write_from_parquet)(
        std::string,
        std::string,
        const BffWriteOptions&);
    BffDataset* (*write_from_parquet_tables)(
        const ParquetTables*,
        std::string,
        const BffWriteOptions&);
};
