// Driver for the generated typed output struct (tests/test_output_struct.py).
// Includes a query_out.hpp generated for the FIXED columns below, fills Q1Out with
// known values, converts to a typed arrow::Table, and writes it to shm for Python.
// Columns (must match the test's codegen input):
//   l_returnflag VARCHAR, sum_qty BIGINT, avg_price DOUBLE, count_order BIGINT
#include "query_out.hpp"
#include "shm_arrow_writer.hpp"
#include <iostream>

int main(int argc, char** argv) {
    if (argc < 2) { std::cerr << "need out path\n"; return 2; }
    Q1Out out;
    out.l_returnflag = {"A", "N", "R"};
    out.sum_qty = {37, 99, 12};
    out.avg_price = {1.5, 2.25, 3.0};
    out.count_order = {3, 5, 2};
    auto tbl = to_arrow_q1(out);
    if (!tbl.ok()) { std::cerr << "ERROR: " << tbl.status().ToString() << "\n"; return 1; }
    synnodb::WriteArrowTableToShm(*tbl, argv[1]);
    std::cout << "ok\n";
    return 0;
}
