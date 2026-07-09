#pragma once

#include <string>
#include <vector>

// FILE_VERSION: 4

// Per-query result returned by query().
// Contains the trace data collected only during this query's execution,
// the wall-clock runtime in fractional milliseconds (measured at microsecond
// resolution, so sub-millisecond work is reported precisely instead of being
// quantized to whole milliseconds), and a non-empty error message when the
// query threw (otherwise empty).
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
std::vector<QueryResult> query(Database*, const std::vector<std::string>& query_lines);

struct QueryApi {
    std::vector<QueryResult> (*query)(Database*, const std::vector<std::string>&);
};
