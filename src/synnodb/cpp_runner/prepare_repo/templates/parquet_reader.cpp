#include "parquet_reader.hpp"

// Increment file version to invalidate cache when this file is changed. This is needed because this file is included in the generated code and changes to it should trigger regeneration of all code that includes it.
// FILE_VERSION: 2


#include "loader_utils.hpp"
#include "shm_arrow_loader.hpp"
#include "shm_paths.hpp"

#include <stdio.h>
#include <unistd.h>


void destroy_parquet_tables(ParquetTables* tables) {
    delete tables;
}

ParquetTables* load(std::string path) {
    auto tables = new ParquetTables{};

    // start: table-reads
    // Generated for TPC-H
    tables->customer = ReadParquetTable(path + "customer.parquet");
    tables->orders = ReadParquetTable(path + "orders.parquet");
    tables->lineitem = ReadParquetTable(path + "lineitem.parquet");
    tables->part = ReadParquetTable(path + "part.parquet");
    tables->partsupp = ReadParquetTable(path + "partsupp.parquet");
    tables->supplier = ReadParquetTable(path + "supplier.parquet");
    tables->nation = ReadParquetTable(path + "nation.parquet");
    tables->region = ReadParquetTable(path + "region.parquet");
    // end: table-reads

    return tables;
}
