#pragma once

// Increment file version to invalidate cache when this file is changed. This is needed because this file is included in the generated code and changes to it should trigger regeneration of all code that includes it.
// FILE_VERSION: 1


#include <arrow/table.h>
#include <memory>

struct ParquetTables {
    using ArrowTable = std::shared_ptr<arrow::Table>;

    // start: table-defs
    // Generated for TPC-H
    ArrowTable customer;
    ArrowTable orders;
    ArrowTable lineitem;
    ArrowTable part;
    ArrowTable partsupp;
    ArrowTable supplier;
    ArrowTable nation;
    ArrowTable region;
    // end: table-defs
};


ParquetTables* load(std::string);
void destroy_parquet_tables(ParquetTables*);
