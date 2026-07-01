#pragma once

#include <memory>

#include "buffer_pool.hpp"
#include "column_handle.hpp"
#include "parquet_reader.hpp"

struct Database {
    // Owns the pool. Every owning member must be RAII (unique_ptr or a value type with a
    // destructor) so that if build() throws mid-way - an out-of-memory build - destroying a
    // partial Database frees everything already allocated. Do NOT add raw owning pointers here;
    // a raw pointer would leak on the throw path, which no longer runs destroy_database.
    std::unique_ptr<BufferPool> pool;

    // TODO: Column handles for SSD-backed columnar storage.
    //
    // Declare one ColumnHandle<T> per logical column, e.g.:
    //   ColumnHandle<int32_t> l_orderkey;
    //   ColumnHandle<double>  l_extendedprice;
    //   StringColumnHandle    c_name;          // offsets + bytes files
    //
    // All handles share the same BufferPool; construct them with pool.get() (they hold a
    // non-owning BufferPool*). The pool pages data in/out of a fixed RAM budget at query time -
    // do NOT store raw std::vector<T> columns.
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
