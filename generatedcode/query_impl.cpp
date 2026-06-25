#include "query_impl.hpp"
#include "cpu_affinity.hpp"


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

#include "args_parser.hpp"
#include "query1.hpp"
#include "query2.hpp"
#include "query3.hpp"
#include "query4.hpp"
#include "query5.hpp"
#include "query6.hpp"
#include "query7.hpp"
#include "query8.hpp"


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

std::vector<QueryResult> query(Database* db, const std::vector<std::string>& query_lines) {
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

    // Pin the process to CPU core 3 for deterministic, low-noise performance measurements.
    pin_process_to_cpu(3);

    // Call query implementations
    for (std::size_t i = 0; i < requests.size(); ++i) {
        const auto& req = requests[i];
        
        // TRACE_RESET();
        long long elapsed_ms = -1;
        std::string error;
        const std::string prefix =
            "run #" + std::to_string(i + 1) + " Q" + req.query_id+"(" + req.req_id + "): ";
        try {
            if (req.query_id == "1") {
                Q1Args args = parse_q1(req);
                std::vector<std::vector<std::string>> rows;
                auto start = std::chrono::steady_clock::now();
                rows = run_q1(db, args);
                auto end = std::chrono::steady_clock::now();
                elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(end - start).count();
                const std::string filename = "result_" + req.req_id + ".csv";
                write_csv(filename, rows);
            }
            else if (req.query_id == "2") {
                Q2Args args = parse_q2(req);
                std::vector<std::vector<std::string>> rows;
                auto start = std::chrono::steady_clock::now();
                rows = run_q2(db, args);
                auto end = std::chrono::steady_clock::now();
                elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(end - start).count();
                const std::string filename = "result_" + req.req_id + ".csv";
                write_csv(filename, rows);
            }
            else if (req.query_id == "3") {
                Q3Args args = parse_q3(req);
                std::vector<std::vector<std::string>> rows;
                auto start = std::chrono::steady_clock::now();
                rows = run_q3(db, args);
                auto end = std::chrono::steady_clock::now();
                elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(end - start).count();
                const std::string filename = "result_" + req.req_id + ".csv";
                write_csv(filename, rows);
            }
            else if (req.query_id == "4") {
                Q4Args args = parse_q4(req);
                std::vector<std::vector<std::string>> rows;
                auto start = std::chrono::steady_clock::now();
                rows = run_q4(db, args);
                auto end = std::chrono::steady_clock::now();
                elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(end - start).count();
                const std::string filename = "result_" + req.req_id + ".csv";
                write_csv(filename, rows);
            }
            else if (req.query_id == "5") {
                Q5Args args = parse_q5(req);
                std::vector<std::vector<std::string>> rows;
                auto start = std::chrono::steady_clock::now();
                rows = run_q5(db, args);
                auto end = std::chrono::steady_clock::now();
                elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(end - start).count();
                const std::string filename = "result_" + req.req_id + ".csv";
                write_csv(filename, rows);
            }
            else if (req.query_id == "6") {
                Q6Args args = parse_q6(req);
                std::vector<std::vector<std::string>> rows;
                auto start = std::chrono::steady_clock::now();
                rows = run_q6(db, args);
                auto end = std::chrono::steady_clock::now();
                elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(end - start).count();
                const std::string filename = "result_" + req.req_id + ".csv";
                write_csv(filename, rows);
            }
            else if (req.query_id == "7") {
                Q7Args args = parse_q7(req);
                std::vector<std::vector<std::string>> rows;
                auto start = std::chrono::steady_clock::now();
                rows = run_q7(db, args);
                auto end = std::chrono::steady_clock::now();
                elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(end - start).count();
                const std::string filename = "result_" + req.req_id + ".csv";
                write_csv(filename, rows);
            }
            else if (req.query_id == "8") {
                Q8Args args = parse_q8(req);
                std::vector<std::vector<std::string>> rows;
                auto start = std::chrono::steady_clock::now();
                rows = run_q8(db, args);
                auto end = std::chrono::steady_clock::now();
                elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(end - start).count();
                const std::string filename = "result_" + req.req_id + ".csv";
                write_csv(filename, rows);
            }
            else {
                throw std::runtime_error("Unsupported query id: " + req.query_id);
            }
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