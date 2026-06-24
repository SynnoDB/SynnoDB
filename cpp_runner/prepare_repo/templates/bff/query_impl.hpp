#pragma once

// Increment file version to invalidate cache when this file is changed.
// FILE_VERSION: 1
//
// BFF variant of query_impl.hpp. Unlike the OLAP in-memory use-case there is no
// in-memory `Database` to build: the queryable state is the .bff dataset on disk
// (addressed via STORAGE_DIR), so this header does not pull in db_loader.hpp.
// g_database is always null for the BFF use-case; query functions open the BFF
// dataset themselves through the read API.

#include "query_api.hpp"

std::vector<QueryResult> query(Database*, const std::vector<std::string>& query_lines);
