#include "query_impl.hpp"
#include "cpu_affinity.hpp"
#include "crash_handler.hpp"  // turn a run_qN SIGSEGV into a symbolized stack trace
// <<thread_pool_include>>
// <<trace_include>>
// Increment file version to invalidate cache when this file is changed. This is needed because this file is included in the generated code and changes to it should trigger regeneration of all code that includes it.
// FILE_VERSION: 7


#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unistd.h>
#include <vector>
#include <cstdlib>
// <<get_thread_pool_placeholder>>
#include "args_parser.hpp"
#include "result_writer.hpp"  // synnodb::write_result - exact Arrow egress
// <<include_query_headers>>


// <<drop_buffer_and_os_caches_def_start>>
void drop_buffer_and_os_caches(Database* db) {
    // clear the buffer pool
    // <<clear_buffer_pool_call>>
    sync();  // flush dirty pages before drop, otherwise kernel may skip or do partial drop

    // try direct write first (works if running as root)
    {
        std::ofstream out("/proc/sys/vm/drop_caches");
        if (out) {
            out << "3\n";
            out.close();
            if (!out.fail()) return;
        }
    }

    // fall back to sudo tee. `-n` never prompts for a password, so a missing right
    // fails immediately rather than blocking the run on interactive input.
    int rc = std::system("echo 3 | sudo -n tee /proc/sys/vm/drop_caches > /dev/null 2>&1");
    if (rc != 0) {
        // Dropping the OS page cache only sharpens cold-cache timings; it is never
        // required for correctness. Warn once and continue rather than aborting the run.
        static bool warned = false;
        if (!warned) {
            warned = true;
            std::cerr
                << "drop_buffer_and_os_caches: could not drop OS page caches (not root "
                   "and passwordless sudo unavailable); continuing with caches intact. "
                   "Query timings may reflect warm OS caches. To drop caches, run as root "
                   "or add to sudoers: "
                   "'youruser ALL=(ALL) NOPASSWD: /usr/bin/tee /proc/sys/vm/drop_caches'"
                << std::endl;
        }
    }
}
// <<drop_buffer_and_os_caches_def_end>>
std::vector<QueryResult> query(Database* db, const std::vector<std::string>& query_lines) {
    // A fatal memory fault inside a run_qN escapes the per-query try/catch below and
    // kills the child; the handler prints the running query + a symbolized backtrace to
    // stderr (captured by the runner) so the failure is diagnosable, not just "signal 11".
    synnodb::install_crash_handler();

    std::vector<QueryResult> results;
    std::vector<QueryRequest> requests;
    for (const auto& line : query_lines) {
        std::istringstream iss(line);
        std::string query_id =  "0";
        iss >> query_id;
        std::string req_id = "0";
        iss >> req_id;
        if (!iss) {
            continue;
        }
        std::string args_line;
        std::getline(iss, args_line); // everything after inst_hash
        requests.push_back(QueryRequest{query_id, req_id, args_line});
    }

    // <<pin_thread_to_core>>

    // Call query implementations
    for (std::size_t i = 0; i < requests.size(); ++i) {
        const auto& req = requests[i];
        // <<drop_buffer_and_os_caches_call>>
        // TRACE_RESET();
        long long elapsed_ms = -1;
        std::string error;
        const std::string prefix =
            "run #" + std::to_string(i + 1) + " Q" + req.query_id+"(" + req.req_id + "): ";
        // Tag the running query so a crash inside run_qN names it in the stack trace.
        synnodb::set_query_context(prefix.c_str());
        try {
            // <<impl_fn_calls>>
        } catch (const std::exception& e) {
            error = prefix + e.what();
        } catch (...) {
            error = prefix + "unknown exception";
        }
        // TRACE_FLUSH();
        results.push_back(QueryResult{req.query_id, req.req_id, "", elapsed_ms, error});
    }
    return results;
}