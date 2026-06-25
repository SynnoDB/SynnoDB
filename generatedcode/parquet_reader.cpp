#include "parquet_reader.hpp"

// Increment file version to invalidate cache when this file is changed. This is needed because this file is included in the generated code and changes to it should trigger regeneration of all code that includes it.
// FILE_VERSION: 1


#include "loader_utils.hpp"

#include <stdio.h>
#include <unistd.h>


void destroy_parquet_tables(ParquetTables* tables) {
    delete tables;
}

ParquetTables* load(std::string path) {
    auto tables = new ParquetTables{};

    tables->customer = ReadParquetTable(path + "customer.parquet");
    tables->lineitem = ReadParquetTable(path + "lineitem.parquet");
    tables->orders = ReadParquetTable(path + "orders.parquet");
    tables->part = ReadParquetTable(path + "part.parquet");
    tables->supplier = ReadParquetTable(path + "supplier.parquet");

    return tables;
}
