#include "parquet_reader.hpp"

// Increment file version to invalidate cache when this file is changed. This is needed because this file is included in the generated code and changes to it should trigger regeneration of all code that includes it.
// FILE_VERSION: 2


#include <stdio.h>
#include <unistd.h>


void destroy_parquet_tables(ParquetTables* tables) {
    delete tables;
}

ParquetTables* load(std::string path) {
    auto tables = new ParquetTables{};

    // start: table-reads
    // Generated for TPC-H
    tables->customer_path = path + "customer.parquet";
    tables->orders_path = path + "orders.parquet";
    tables->lineitem_path = path + "lineitem.parquet";
    tables->part_path = path + "part.parquet";
    tables->partsupp_path = path + "partsupp.parquet";
    tables->supplier_path = path + "supplier.parquet";
    tables->nation_path = path + "nation.parquet";
    tables->region_path = path + "region.parquet";
    // end: table-reads

    return tables;
}
