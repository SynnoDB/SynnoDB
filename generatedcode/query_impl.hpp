#pragma once

// Increment file version to invalidate cache when this file is changed.
// FILE_VERSION: 1
//
// BFF variant of query_impl.hpp. Unlike the OLAP in-memory use-case there is no
// columnar in-memory `Database` to build: the queryable state is the .bff
// dataset on disk (addressed via STORAGE_DIR), so this header does not pull in
// db_loader.hpp. Instead g_database is a thin handle (see Database in
// bff_format.hpp) holding the open dataset + decoded footer, built once in the
// writer stage and reused across query hot-reloads.

#include "query_api.hpp"

std::vector<QueryResult> query(Database*, const std::vector<std::string>& query_lines);
