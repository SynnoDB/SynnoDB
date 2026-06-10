#pragma once

#include "buffer_pool.hpp"
#include "column_handle.hpp"
#include "parquet_reader.hpp"

struct Database {
    BufferPool* pool = nullptr;

    // TODO: Column handles for SSD-backed columnar storage.
    //
    // Declare one ColumnHandle<T> per logical column, e.g.:
    //   ColumnHandle<int32_t> l_orderkey;
    //   ColumnHandle<double>  l_extendedprice;
    //   StringColumnHandle    c_name;          // offsets + bytes files
    //
    // All handles share the same BufferPool. The pool pages data in/out of a
    // fixed RAM budget at query time — do NOT store raw std::vector<T> columns.
};


// build() opens the Parquet file paths stored in `tables`, streams the needed
// columns to flat binary files in the directory given by STORAGE_DIR
// (default: "./column_files/"), then opens those files via the BufferPool and
// returns a Database populated with ColumnHandle<T> descriptors.
//
// build() is called once per process; query() is called repeatedly against
// the returned Database*.
Database* build(ParquetTables* tables);
void destroy_database(Database*);
