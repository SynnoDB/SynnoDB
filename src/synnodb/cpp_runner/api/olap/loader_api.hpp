#pragma once

#include <string>

#include "../plugin_abi.h"

// FILE_VERSION: 2

struct ParquetTables;

// Implemented by the generated parquet_reader.cpp. The C entry points that
// actually cross the .so boundary live in loader_api.cpp.
ParquetTables* load(std::string);
void destroy_parquet_tables(ParquetTables*);
