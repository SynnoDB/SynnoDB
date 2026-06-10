#pragma once

// Increment file version to invalidate cache when this file is changed. This is needed because this file is included in the generated code and changes to it should trigger regeneration of all code that includes it.
// FILE_VERSION: 1

#include <iomanip>
#include <string>
#include <sstream>
#include <vector>

struct QueryRequest {
    std::string query_id; // query-id 
    std::string req_id; // id of the request
    std::string line; // line with query arguments
};

${query_structs_and_parsers}
