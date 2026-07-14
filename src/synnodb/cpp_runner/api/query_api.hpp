#pragma once

#include <string>
#include <vector>

#include "plugin_abi.h"

// FILE_VERSION: 5

// Per-query result returned by query().
// Contains the trace data collected only during this query's execution,
// the wall-clock runtime in fractional milliseconds (measured at microsecond
// resolution, so sub-millisecond work is reported precisely instead of being
// quantized to whole milliseconds), and a non-empty error message when the
// query threw (otherwise empty).
//
// This is the C++-side result type, produced by the generated query_impl.cpp.
// It is not what crosses the .so boundary: query_api.cpp converts it to the C
// SynnoQueryResult in plugin_abi.h. Keeping the two apart lets the generated
// code stay idiomatic C++ while the ABI stays language-neutral.
struct QueryResult {
    std::string query_id;
    std::string req_id;
    std::string trace;
    double      elapsed_ms;
    std::string error;
};

struct Database;

// query_lines are now passed directly from the RunBatch rather than being read
// from stdin, so each invocation operates on exactly the lines that were sent
// with the corresponding RUN command.
//
// Implemented by the generated query_impl.cpp.
std::vector<QueryResult> query(Database*, const std::vector<std::string>& query_lines);
