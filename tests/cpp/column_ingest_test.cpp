// Validates the delegation-based column ingestion. Two modes:
//   lineitem <parquet>  -> TPC-H sums (checked vs DuckDB)
//   synth    <parquet>  -> diverse types the old type-switch missed:
//                          decimal, int, BOOL, DICTIONARY-string, TIMESTAMP, date, double
#include "column_ingest.hpp"

#include <arrow/io/file.h>
#include <parquet/arrow/reader.h>
#include <iostream>
#include <memory>
#include <string>

static std::shared_ptr<arrow::Table> read_parquet(const std::string& p) {
    auto infile = arrow::io::ReadableFile::Open(p).ValueOrDie();
    auto rr = parquet::arrow::OpenFile(infile, arrow::default_memory_pool());
    if (!rr.ok()) { std::cerr << rr.status().ToString() << "\n"; std::exit(1); }
    auto reader = std::move(rr).ValueOrDie();
    std::shared_ptr<arrow::Table> table;
    auto st = reader->ReadTable(&table);
    if (!st.ok()) { std::cerr << st.ToString() << "\n"; std::exit(1); }
    return table;
}

int main(int argc, char** argv) {
    if (argc < 3) { std::cerr << "usage: <lineitem|synth> <parquet>\n"; return 2; }
    std::string mode = argv[1];
    auto table = read_parquet(argv[2]);
    using namespace synnodb::ingest;
    try {
        if (mode == "lineitem") {
            auto qty = scaled_int64(*table, "l_quantity", 2);
            auto ep = scaled_int64(*table, "l_extendedprice", 2);
            auto okey = as_int64(*table, "l_orderkey");
            auto rf = as_string(*table, "l_returnflag");
            auto sd = as_date_days(*table, "l_shipdate");
            long long sq = 0, se = 0, so = 0, ca = 0;
            for (auto v : qty) sq += v;
            for (auto v : ep) se += v;
            for (auto v : okey) so += v;
            for (auto& s : rf) if (s == "A") ++ca;
            int mn = 2147483647, mx = -2147483648;
            for (auto d : sd) { if (d < mn) mn = d; if (d > mx) mx = d; }
            std::cout << "rows=" << table->num_rows() << " sum_qty=" << sq << " sum_ep=" << se
                      << " sum_okey=" << so << " rf_A=" << ca << " sd_min=" << mn << " sd_max=" << mx << "\n";
        } else {  // synth: decimal/int/bool/dictionary/timestamp/date/double
            auto dec = scaled_int64(*table, "dec_col", 2);
            auto iv = as_int64(*table, "int_col");
            auto bv = as_int64(*table, "bool_col");
            auto sv = as_string(*table, "dict_col");
            auto tv = as_date_days(*table, "ts_col");
            auto dv = as_date_days(*table, "date_col");
            auto fv = as_double(*table, "dbl_col");
            long long sdec = 0, siv = 0, sbv = 0, cA = 0;
            for (auto x : dec) sdec += x;
            for (auto x : iv) siv += x;
            for (auto x : bv) sbv += x;
            for (auto& s : sv) if (s == "A") ++cA;
            double sf = 0; for (auto x : fv) sf += x;
            std::cout << "dec=" << sdec << " int=" << siv << " bool=" << sbv << " dictA=" << cA
                      << " ts0=" << tv[0] << " date0=" << dv[0] << " dbl=" << sf << "\n";
        }
    } catch (const std::exception& e) { std::cerr << "ERROR: " << e.what() << "\n"; return 1; }
    return 0;
}
