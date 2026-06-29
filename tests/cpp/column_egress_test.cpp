// Validates the delegation-based column egress (cpp_helpers/column_egress.hpp). Two modes:
//   build    <out.arrow>  -> build a table covering the type matrix egress must emit
//                            (int widths, float widths, bool, string, decimal128/256, date,
//                            timestamp) plus NULLs, written via the real WriteArrowTableToShm
//                            path; Python reads it back and asserts exact types/values/nulls.
//   overflow <out.arrow>  -> emit a BIGINT value that does not fit the requested INTEGER
//                            target; the safe Cast must THROW (loud), not truncate.
#include "column_egress.hpp"
#include "shm_arrow_writer.hpp"

#include <cstdint>
#include <iostream>
#include <string>
#include <vector>

int main(int argc, char** argv) {
    if (argc < 3) { std::cerr << "usage: <build|overflow> <out.arrow>\n"; return 2; }
    const std::string mode = argv[1];
    const std::string out = argv[2];
    using namespace synnodb::egress;
    try {
        if (mode == "overflow") {
            // 10_000_000_000 does not fit int32; the safe Cast must fail loudly.
            (void)int64_column({10000000000LL}, {}, arrow::int32());
            std::cerr << "ERROR: expected overflow cast to throw\n";
            return 3;
        }
        if (mode == "decimal_overflow") {
            // 10^38 (39 digits) does not fit DECIMAL(38,0); the precision guard must throw.
            __int128 p38 = 1;
            for (int i = 0; i < 38; ++i) p38 *= 10;
            (void)decimal_column({p38}, 38, 0);
            std::cerr << "ERROR: expected decimal precision overflow to throw\n";
            return 3;
        }
        if (mode == "length_mismatch") {
            // Columns of different lengths must be rejected by make_table, not silently built.
            (void)make_table({{"a", int64_column({1, 2, 3})}, {"b", int64_column({9})}});
            std::cerr << "ERROR: expected make_table length mismatch to throw\n";
            return 3;
        }

        // build: one row per index, NULLs at the indices marked 0 in the validity masks.
        const Validity v = {1, 0, 1, 0};  // present, NULL, present, NULL
        const std::vector<uint64_t> ubig_vals = {
            0ULL,
            9223372036854775808ULL,
            18446744073709551615ULL,
            4ULL,
        };
        const std::vector<__int128> huge_vals = {
            static_cast<__int128>(1) << 100,
            -(static_cast<__int128>(1) << 100),
            0,
            42,
        };

        auto table = make_table({
            {"bigint",   int64_column({1, 2, -3, 4})},                              // BIGINT
            {"integer",  integer_column(std::vector<int32_t>{10, 20, 30, 40}, {}, arrow::int32())}, // INTEGER
            {"smallint", integer_column(std::vector<int16_t>{1, 2, 3, 4}, {}, arrow::int16())},     // SMALLINT
            {"tinyint",  integer_column(std::vector<int8_t>{1, 2, -3, 4}, {}, arrow::int8())},      // TINYINT
            {"ubigint",  uint64_column(ubig_vals)},                                  // UBIGINT
            {"dbl",      double_column({1.5, 2.5, 3.0, 4.0})},                      // DOUBLE
            {"real",     double_column({1.5, 2.5, 3.0, 4.0}, {}, arrow::float32())},// REAL (narrowed)
            {"flag",     bool_column({true, false, true, true})},                   // BOOLEAN
            {"name",     string_column({"a", "b", "c", "d"})},                      // VARCHAR
            {"dec",      decimal_column({150, -225, 0, 1010}, 38, 2)},              // DECIMAL(38,2)
            {"hugeint",  hugeint_column(huge_vals)},                                // HUGEINT -> decimal128(38,0)
            {"wide",     decimal_column({150, -225, 0, 1010}, 50, 2)},              // DECIMAL(50,2) -> decimal256
            {"d",        date_column({19359, 19360, 19361, 19362})},                // DATE
            {"ts",       timestamp_column({1000000, 2000000, 3000000, 4000000})},   // TIMESTAMP[us]
            {"nul_str",  string_column({"x", "", "z", ""}, v)},                     // VARCHAR with NULLs
            {"nul_int",  int64_column({5, 0, 7, 0}, v)},                            // BIGINT with NULLs
            {"nul_dec",  decimal_column({100, 0, 300, 0}, 38, 2, v)},               // DECIMAL with NULLs
            {"nul_ts",   timestamp_column({1000000, 0, 3000000, 0},                 // TIMESTAMP with NULLs
                                          arrow::TimeUnit::MICRO, v)},
        });

        synnodb::WriteArrowTableToShm(table, out);
        std::cout << "rows=" << table->num_rows() << " cols=" << table->num_columns() << "\n";
    } catch (const std::exception& e) {
        std::cerr << "ERROR: " << e.what() << "\n";
        return 1;
    }
    return 0;
}
