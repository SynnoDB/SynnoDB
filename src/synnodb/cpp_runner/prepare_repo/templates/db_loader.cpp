#include "db_loader.hpp"

#include <memory>


Database* build(ParquetTables*) {
    // Own the partial dataset while it is being built: if any step below throws (a large build
    // running out of memory is the expected case), unwinding destroys db and frees everything it
    // owns. Ownership is handed to the caller only once build() succeeds.
    auto db = std::make_unique<Database>();

    // TODO: implement the build logic to convert ParquetTables into an efficient in-memory data structure

    return db.release();
}

void destroy_database(Database* db) {
    delete db;
}
