#pragma once

// FILE_VERSION: 1

struct ParquetTables;
struct Database;

Database* build(ParquetTables*);
void destroy_database(Database*);

struct BuilderApi {
    Database* (*build)(ParquetTables*);
    void (*destroy)(Database*);
};
