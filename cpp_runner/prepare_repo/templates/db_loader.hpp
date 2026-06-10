#pragma once

#include "parquet_reader.hpp"

struct Database {
    // TODO: Data structures to hold the in-memory representation of the data
};


Database* build(ParquetTables*);
void destroy_database(Database*);
