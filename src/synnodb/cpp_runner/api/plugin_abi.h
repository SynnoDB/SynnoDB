#pragma once

// FILE_VERSION: 1

// The stable, language-neutral ABI between the host (db.cpp + hotpatch/) and the
// three engine plugins (libloader.so, libbuilder.so, libquery.so).
//
// Every type that crosses a .so boundary is a plain C type, so a plugin can be
// written in any language with a C FFI. The C++ plugins keep their C++ internals
// and convert in the api/*.cpp shims; a Rust plugin implements these structs
// directly as #[repr(C)].
//
// Handles (ParquetTables, Database) are opaque void*: the host only carries them
// from one stage to the next and never dereferences them. Each api struct pairs
// its producer with a destroy function, so a handle is always freed by the same
// plugin that allocated it -- ownership never crosses the boundary.
//
// No error may propagate out of a plugin as a language-level exception/panic:
// unwinding across this boundary is undefined. A plugin catches its own failures
// and reports them as data (SynnoQueryBatchResult::stage_error).

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

// One query's outcome. Strings are NUL-terminated, never null (an absent value
// is the empty string), and owned by the plugin -- they stay valid until
// free_results() is called on the containing batch.
typedef struct SynnoQueryResult {
    const char* query_id;
    const char* req_id;
    const char* trace;
    const char* error;  // empty when the query itself succeeded
    double      elapsed_ms;
} SynnoQueryResult;

// The outcome of one RUN batch. stage_error is non-empty when the batch failed
// as a whole (as opposed to an individual query failing, which is reported in
// that query's SynnoQueryResult::error); results may then be empty.
typedef struct SynnoQueryBatchResult {
    SynnoQueryResult* results;
    size_t            len;
    const char*       stage_error;
} SynnoQueryBatchResult;

// A failing load()/build() returns NULL and leaves a message in last_error().
// The host turns that into a stage failure on its own side of the boundary, so
// the error text survives without an exception ever crossing it. last_error()
// returns a plugin-owned string, valid until the next call into that plugin,
// and never null (no failure recorded is the empty string).

typedef struct SynnoLoaderApi {
    // Read the dataset at parquet_dir. Returns an opaque ParquetTables handle,
    // or NULL on failure.
    void*       (*load)(const char* parquet_dir);
    void        (*destroy)(void* parquet_tables);
    const char* (*last_error)(void);
} SynnoLoaderApi;

typedef struct SynnoBuilderApi {
    // Turn the loader's tables into the engine's storage layout. Returns an
    // opaque Database handle, or NULL on failure.
    void*       (*build)(void* parquet_tables);
    void        (*destroy)(void* database);
    const char* (*last_error)(void);
} SynnoBuilderApi;

typedef struct SynnoQueryApi {
    // Run n_lines query lines against database, filling *out. The plugin owns
    // everything reachable from *out until free_results(out) is called.
    void (*query)(void*                   database,
                  const char* const*      lines,
                  size_t                  n_lines,
                  SynnoQueryBatchResult*  out);
    void (*free_results)(SynnoQueryBatchResult* out);
} SynnoQueryApi;

#ifdef __cplusplus
}  // extern "C"
#endif
