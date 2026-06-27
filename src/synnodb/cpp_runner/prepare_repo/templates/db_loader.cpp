#include "db_loader.hpp"


Database* build(ParquetTables*) {
    // TODO: implement the build logic to convert ParquetTables into an efficient in-memory data structure
    return new Database{};
}

void destroy_database(Database* db) {
    delete db;
}
