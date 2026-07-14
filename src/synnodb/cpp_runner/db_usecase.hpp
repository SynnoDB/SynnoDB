#pragma once

// Interface header for use-case specific DB implementations.
//
// db.cpp includes this for the shared infrastructure (query stage, globals).
// Each use-case file (e.g. db_olap.cpp, db_etl.cpp) implements the two
// functions declared at the bottom.

#include "pipeline.hpp"
#include "query_api.hpp"
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

// g_database:       owned by the use-case; set by the ingest pipeline, read by
//                   the query stage. An opaque handle: the host carries it from
//                   the builder to the query stage and never dereferences it,
//                   which is what lets the plugins be written in any language
//                   with a C FFI (see api/plugin_abi.h).
// g_trace_write_fd: owned by db.cpp; set in main() before fork.
extern void* g_database;
extern int   g_trace_write_fd;

// Utility functions defined in db.cpp.
std::string json_escape(const std::string& s);
void        write_length_prefixed(int fd, const std::string& data);

// Returns a batch's results to the plugin that allocated them. Memory allocated
// behind the ABI must be freed behind it too -- the host's allocator is not
// necessarily the plugin's.
struct ResultsGuard {
    SynnoQueryApi          api;
    SynnoQueryBatchResult* results;

    ResultsGuard(const SynnoQueryApi& api_, SynnoQueryBatchResult* results_)
        : api(api_), results(results_) {}

    ResultsGuard(const ResultsGuard&) = delete;
    ResultsGuard& operator=(const ResultsGuard&) = delete;

    ~ResultsGuard() {
        if (api.free_results && results)
            api.free_results(results);
    }
};

// The query stage lives here so every use-case gets the same implementation
// without duplicating it.  It reads g_database and g_trace_write_fd.
inline auto make_query_stage() {
    // The query stage receives batch.query_lines from run_parent via the
    // framed IPC protocol.  Previously the lines were written to stdin
    // before the RUN signal, which could leave stale lines buffered and
    // cause them to be consumed by a subsequent invocation.
    return stage<RunPolicy::AlwaysReload>("./build/libquery.so",
        [](Plugin& plugin, int, const RunBatch& batch) {
            auto api = plugin.get<SynnoQueryApi>();
            std::cerr << "query start\n";

            // The plugin reports a failing batch as data (batch.stage_error)
            // rather than by throwing: an exception must not unwind across the
            // C ABI. The child process therefore still emits a structured
            // response instead of aborting. Note this does NOT cover async
            // signals like SIGSEGV; those still terminate the child and are
            // reported via term_signal.
            std::vector<const char*> lines;
            lines.reserve(batch.query_lines.size());
            for (const auto& line : batch.query_lines)
                lines.push_back(line.c_str());

            SynnoQueryBatchResult results{};
            api.query(g_database, lines.data(), lines.size(), &results);
            // The plugin owns everything reachable from `results`; hand it back
            // on every path out of this scope, including a throw from the
            // payload building below.
            ResultsGuard guard{api, &results};

            if (results.stage_error && results.stage_error[0] != '\0')
                std::cerr << results.stage_error << "\n";
            std::cerr << "query done\n";

            // Serialize per-query results plus any stage-level error as a JSON
            // object and send to run_parent via the trace pipe, before
            // write_done() fires on done_pipe.
            if (g_trace_write_fd >= 0) {
                std::string payload = "{\"query_results\":[";
                for (std::size_t i = 0; i < results.len; ++i) {
                    const SynnoQueryResult& r = results.results[i];
                    if (i > 0) payload += ",";
                    payload += "{\"trace\":\"";
                    payload += json_escape(r.trace);
                    payload += "\",\"elapsed_ms\":";
                    payload += std::to_string(r.elapsed_ms);
                    payload += ",\"error\":\"";
                    payload += json_escape(r.error);
                    payload += "\",\"query_id\":\"";
                    payload += json_escape(r.query_id);
                    payload += "\",\"req_id\":\"";
                    payload += json_escape(r.req_id);
                    payload += "\"}";
                }
                payload += "],\"stage_error\":\"";
                payload += json_escape(results.stage_error);
                payload += "\"}";
                write_length_prefixed(g_trace_write_fd, payload);
            }
            return 0;
        });
}

// ---------------------------------------------------------------------------
// Each use-case file must implement these two functions.
// ---------------------------------------------------------------------------

// Parse use-case specific command-line arguments.
// Prints usage and returns false on error; db.cpp exits with code 1 if false.
bool usecase_parse_args(int argc, char** argv);

// Build and run the full child pipeline (ingest stages + make_query_stage()).
// Called in the forked child process.
void usecase_run_child(int read_fd, int done_fd);
