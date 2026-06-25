#pragma once

#include <string>
#include <vector>

#include "args_parser.hpp"
#include "query_api.hpp"

// BFF query entry point. `db` is the query-time handle built once in the writer
// stage (open .bff dataset + decoded footer); see Database in bff_format.hpp.
// Read pruning metadata from db->footer and the dataset via db->dataset.
std::vector<std::vector<std::string>> run_q3(Database* db, const Q3Args& args);
