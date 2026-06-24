#pragma once

#include <string>
#include <vector>

#include "args_parser.hpp"
#include "query_api.hpp"

// BFF query entry point. `db` is always null for the BFF use-case (the dataset
// lives on disk, addressed via STORAGE_DIR); it is kept in the signature only to
// match the shared query_impl.cpp dispatch.
std::vector<std::vector<std::string>> run_q${qid}(Database* db, const Q${qid}Args& args);
