#pragma once

#include "../plugin_abi.h"

// FILE_VERSION: 2

struct ParquetTables;
struct Database;

// Implemented by the generated db_loader.cpp. The C entry points that actually
// cross the .so boundary live in builder_api.cpp.
Database* build(ParquetTables*);
void destroy_database(Database*);
