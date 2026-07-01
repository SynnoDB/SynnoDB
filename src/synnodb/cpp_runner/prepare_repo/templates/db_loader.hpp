#pragma once

#include "parquet_reader.hpp"

struct Database {
    // TODO: Data structures to hold the in-memory representation of the data.
    //
    // Every owning member must be RAII (unique_ptr, std::vector, or a value type with a
    // destructor) so that if build() throws mid-way - an out-of-memory build - destroying a
    // partial Database frees everything already allocated. Do NOT add raw owning pointers here;
    // a raw pointer would leak on the throw path, which no longer runs destroy_database.
};


Database* build(ParquetTables*);
void destroy_database(Database*);
