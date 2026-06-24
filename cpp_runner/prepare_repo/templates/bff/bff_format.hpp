#pragma once

// Increment file version to invalidate cache when this file is changed.
// FILE_VERSION: 1
//
// Concrete definitions of the opaque BFF handles that ingest_types.hpp only
// forward-declares (BffDataset / BffTable / BffFooter). These minimal skeleton
// definitions exist so the read/write/query implementations compile and the
// loader -> writer -> query pipeline runs end to end.
//
// The agent is expected to flesh these out with the real on-disk format state
// (footer cache, file descriptors / mmap regions, per-row-group page indexes,
// dictionaries, etc.) as part of implementing the read and write APIs.

#include "ingest_types.hpp"

#include <string>

struct BffFooter {
    // Decoded footer metadata for a dataset (schema, row groups, pages, stats).
    BffFooterInfo info;
};

struct BffDataset {
    std::string root_path;          // directory holding the .bff table files
    BffOpenOptions options;         // options the dataset was opened with
    bool has_footer = false;        // whether `footer` has been populated
    BffFooter footer;               // cached footer (see cache_footer option)
};

struct BffTable {
    BffDataset* dataset = nullptr;  // owning dataset (not owned)
    BffTableInfo info;              // per-table schema + row group metadata
};

// Query-time handle held by the host as `g_database` (the opaque `Database` of
// query_api.hpp). For the BFF use-case the queryable state is the on-disk
// dataset, so `Database` is simply the already-opened, footer-loaded read
// handle. It is built once in the writer stage via build_bff_query_database()
// and survives query-plugin hot-reloads, so every run_q<N>() sees the decoded
// footer without re-opening the dataset or re-parsing the footer per query.
//
// This is framework plumbing: you normally extend BffDataset / BffFooter rather
// than this struct.
struct Database {
    BffDataset* dataset = nullptr;      // open read handle (owned by the Database)
    const BffFooter* footer = nullptr;  // cached footer (points into *dataset)
};
