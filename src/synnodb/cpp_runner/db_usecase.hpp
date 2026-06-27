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
//                   the query stage.
// g_trace_write_fd: owned by db.cpp; set in main() before fork.
extern Database* g_database;
extern int       g_trace_write_fd;

// Utility functions defined in db.cpp.
std::string json_escape(const std::string& s);
void        write_length_prefixed(int fd, const std::string& data);

// The query stage lives here so every use-case gets the same implementation
// without duplicating it.  It reads g_database and g_trace_write_fd.
inline auto make_query_stage() {
    // The query stage receives batch.query_lines from run_parent via the
    // framed IPC protocol.  Previously the lines were written to stdin
    // before the RUN signal, which could leave stale lines buffered and
    // cause them to be consumed by a subsequent invocation.
    return stage<RunPolicy::AlwaysReload>("./build/libquery.so",
        [](Plugin& plugin, int, const RunBatch& batch) {
            auto api = plugin.get<QueryApi>();
            std::cerr << "query start\n";

            // Catch any C++ exception thrown out of api.query() so the child
            // process can still report a structured response instead of
            // aborting (which would surface only as a SIGABRT to the parent).
            // Note: this does NOT catch async signals like SIGSEGV; those
            // still terminate the child and are reported via term_signal.
            std::vector<QueryResult> results;
            std::string stage_error;
            try {
                results = api.query(g_database, batch.query_lines);
            } catch (const std::exception& e) {
                stage_error = std::string("query stage threw std::exception: ") + e.what();
                std::cerr << stage_error << "\n";
            } catch (...) {
                stage_error = "query stage threw unknown exception";
                std::cerr << stage_error << "\n";
            }
            std::cerr << "query done\n";
            
            // Serialize per-query results plus any stage-level error as a JSON
            // object and send to run_parent via the trace pipe, before
            // write_done() fires on done_pipe.
            if (g_trace_write_fd >= 0) {
                std::string payload = "{\"query_results\":[";
                for (std::size_t i = 0; i < results.size(); ++i) {
                    if (i > 0) payload += ",";
                    payload += "{\"trace\":\"";
                    payload += json_escape(results[i].trace);
                    payload += "\",\"elapsed_ms\":";
                    payload += std::to_string(results[i].elapsed_ms);
                    payload += ",\"error\":\"";
                    payload += json_escape(results[i].error);
                    payload += "\",\"query_id\":\"";
                    payload += json_escape(results[i].query_id);
                    payload += "\",\"req_id\":\"";
                    payload += json_escape(results[i].req_id);
                    payload += "\"}";
                }
                payload += "],\"stage_error\":\"";
                payload += json_escape(stage_error);
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
