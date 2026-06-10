#include "query_impl.hpp"
#include "cpu_affinity.hpp"
// <<thread_pool_include>>
// Increment file version to invalidate cache when this file is changed. This is needed because this file is included in the generated code and changes to it should trigger regeneration of all code that includes it.
// FILE_VERSION: 6


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
// <<include_query_headers>>


void write_csv(const std::string& filename, const std::vector<std::vector<std::string>>& rows) {
    std::filesystem::create_directories("results");
    std::ofstream out("results/" + filename);
    for (const auto& row : rows) {
        for (std::size_t i = 0; i < row.size(); ++i) {
            if (i) out << ',';
            out << '"';
            for (char c : row[i]) {
                if (c == '"' || c == '\\') out << '\\';
                out << c;
            }
            out << '"';
        }
        out << '\n';
    }
}
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

    // fall back to sudo tee
    int rc = std::system("echo 3 | sudo -n tee /proc/sys/vm/drop_caches > /dev/null 2>&1");
    if (rc != 0) {
        throw std::runtime_error(
            "drop_buffer_and_os_caches: failed to drop caches (not root and sudo -n tee failed). "
            "Add to sudoers: 'youruser ALL=(ALL) NOPASSWD: /usr/bin/tee /proc/sys/vm/drop_caches'"
        );
    }
}
// <<drop_buffer_and_os_caches_def_end>>
std::vector<QueryResult> query(Database* db, const std::vector<std::string>& query_lines) {
    std::vector<QueryResult> results;
    std::vector<QueryRequest> requests;
    for (const auto& line : query_lines) {
        std::istringstream iss(line);
        std::string query_id =  "0";
        iss >> query_id;
        if (!iss) {
            continue;
        }
        requests.push_back(QueryRequest{query_id, line});
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
            "run #" + std::to_string(i + 1) + " Q" + req.id + ": ";
        try {
            // <<impl_fn_calls>>
        } catch (const std::exception& e) {
            error = prefix + e.what();
        } catch (...) {
            error = prefix + "unknown exception";
        }
        // TRACE_FLUSH();
        results.push_back({"", elapsed_ms, error});
    }
    return results;
}