#pragma once

#include <string>

// FILE_VERSION: 1

struct ParquetTables;

ParquetTables* load(std::string);
void destroy_parquet_tables(ParquetTables*);

struct LoaderApi {
    ParquetTables* (*load)(std::string);
    void (*destroy)(ParquetTables*);
};
