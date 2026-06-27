#pragma once

// Increment file version to invalidate cache when this file is changed. This is needed because this file is included in the generated code and changes to it should trigger regeneration of all code that includes it.
// FILE_VERSION: 1


#include <string>

struct ParquetTables {
    // start: table-defs
    // Generated for TPC-H
    std::string customer_path;
    std::string orders_path;
    std::string lineitem_path;
    std::string part_path;
    std::string partsupp_path;
    std::string supplier_path;
    std::string nation_path;
    std::string region_path;
    // end: table-defs
};


ParquetTables* load(std::string);
void destroy_parquet_tables(ParquetTables*);
