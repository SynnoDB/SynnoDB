#pragma once
// column_ingest.hpp — read Arrow columns into typed C++ vectors.
//
// Background (docs/CAUGHT_ERRORS_IN_GENERATION.md, G2): decoding Arrow buffers by hand is
// easy to get wrong. A hand-written DECIMAL128 FromBigEndian decode read every value
// column as zero (count was right, the sums were all zero), and a switch over Arrow types
// keeps missing the case the next workload happens to use (TIMESTAMP, DICTIONARY, BOOLEAN,
// DECIMAL256, ...).
//
// Each helper here casts the column to a single canonical Arrow type with
// arrow::compute::Cast and then reads only that type. Cast already covers the full set of
// Arrow types, so there is no per-type switch to maintain; a cast it cannot do (or a value
// that overflows the target) throws instead of returning a wrong number.
//
// db_loader.cpp build() calls these and does not touch Arrow itself:
//   using namespace synnodb::ingest;
//   db->l_quantity      = scaled_int64(*tables->lineitem, "l_quantity", 2); // fixed-point
//   db->l_orderkey      = as_int64    (*tables->lineitem, "l_orderkey");
//   db->l_returnflag    = as_string   (*tables->lineitem, "l_returnflag");
//   db->l_shipdate_days = as_date_days(*tables->lineitem, "l_shipdate");
//
// Tests: tests/test_column_ingest.py (TPC-H parquet vs DuckDB),
// tests/cpp/column_ingest_test.cpp (decimal/bool/dictionary/timestamp).

#include <cstdint>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#include <arrow/api.h>
#include <arrow/compute/api.h>

namespace synnodb {
namespace ingest {

inline std::shared_ptr<arrow::ChunkedArray> column(const arrow::Table& t, const std::string& name) {
    const int idx = t.schema()->GetFieldIndex(name);
    if (idx < 0) throw std::runtime_error("column_ingest: column not found: " + name);
    return t.column(idx);
}

// Cast a column to `target`. Throws if Arrow cannot perform the cast.
inline std::shared_ptr<arrow::ChunkedArray> canonicalize(
    const std::shared_ptr<arrow::ChunkedArray>& col, std::shared_ptr<arrow::DataType> target) {
    if (col->type()->Equals(*target)) return col;
    auto result = arrow::compute::Cast(arrow::Datum(col), std::move(target), arrow::compute::CastOptions::Safe());
    if (!result.ok()) {
        throw std::runtime_error("column_ingest: cannot canonicalize column of type " +
                                 col->type()->ToString() + ": " + result.status().ToString());
    }
    return result->chunked_array();
}

// ---- as_int64: any integer/bool/date source -> int64 -------------------------
inline std::vector<int64_t> as_int64(const std::shared_ptr<arrow::ChunkedArray>& col) {
    auto c = canonicalize(col, arrow::int64());
    std::vector<int64_t> out;
    out.reserve(static_cast<size_t>(c->length()));
    for (const auto& chunk : c->chunks()) {
        const auto& a = static_cast<const arrow::Int64Array&>(*chunk);
        for (int64_t i = 0; i < a.length(); ++i) out.push_back(a.IsNull(i) ? 0 : a.Value(i));
    }
    return out;
}
inline std::vector<int64_t> as_int64(const arrow::Table& t, const std::string& name) { return as_int64(column(t, name)); }

// ---- as_double: any numeric/decimal source -> double -------------------------
inline std::vector<double> as_double(const std::shared_ptr<arrow::ChunkedArray>& col) {
    auto c = canonicalize(col, arrow::float64());
    std::vector<double> out;
    out.reserve(static_cast<size_t>(c->length()));
    for (const auto& chunk : c->chunks()) {
        const auto& a = static_cast<const arrow::DoubleArray&>(*chunk);
        for (int64_t i = 0; i < a.length(); ++i) out.push_back(a.IsNull(i) ? 0.0 : a.Value(i));
    }
    return out;
}
inline std::vector<double> as_double(const arrow::Table& t, const std::string& name) { return as_double(column(t, name)); }

// ---- scaled_int64: numeric/decimal source -> fixed-point int64 (value*10^scale) -----
// Cast to decimal(38,scale) so Arrow does the rescale, then read each value's unscaled
// integer. A value that does not fit int64 at this scale throws rather than truncating.
inline std::vector<int64_t> scaled_int64(const std::shared_ptr<arrow::ChunkedArray>& col, int scale) {
    auto c = canonicalize(col, arrow::decimal128(38, scale));
    std::vector<int64_t> out;
    out.reserve(static_cast<size_t>(c->length()));
    for (const auto& chunk : c->chunks()) {
        const auto& a = static_cast<const arrow::Decimal128Array&>(*chunk);
        for (int64_t i = 0; i < a.length(); ++i) {
            if (a.IsNull(i)) { out.push_back(0); continue; }
            const arrow::Decimal128 d(a.GetValue(i));   // native little-endian; the unscaled value
            const int64_t hi = d.high_bits();
            const int64_t v = static_cast<int64_t>(d.low_bits());
            const bool fits = (hi == 0 && v >= 0) || (hi == -1 && v < 0);
            if (!fits) {
                throw std::runtime_error(
                    "column_ingest::scaled_int64: value does not fit int64 at scale=" +
                    std::to_string(scale) + " (use as_double, or a wider accumulator type)");
            }
            out.push_back(v);
        }
    }
    return out;
}
inline std::vector<int64_t> scaled_int64(const arrow::Table& t, const std::string& name, int scale) {
    return scaled_int64(column(t, name), scale);
}

// ---- as_string: any string/dictionary/etc. source -> std::string -------------
inline std::vector<std::string> as_string(const std::shared_ptr<arrow::ChunkedArray>& col) {
    auto c = canonicalize(col, arrow::utf8());
    std::vector<std::string> out;
    out.reserve(static_cast<size_t>(c->length()));
    for (const auto& chunk : c->chunks()) {
        const auto& a = static_cast<const arrow::StringArray&>(*chunk);
        for (int64_t i = 0; i < a.length(); ++i) out.emplace_back(a.IsNull(i) ? std::string() : a.GetString(i));
    }
    return out;
}
inline std::vector<std::string> as_string(const arrow::Table& t, const std::string& name) { return as_string(column(t, name)); }

// ---- as_date_days: any date/timestamp source -> int32 days since 1970-01-01 ---
inline std::vector<int32_t> as_date_days(const std::shared_ptr<arrow::ChunkedArray>& col) {
    auto c = canonicalize(col, arrow::date32());
    std::vector<int32_t> out;
    out.reserve(static_cast<size_t>(c->length()));
    for (const auto& chunk : c->chunks()) {
        const auto& a = static_cast<const arrow::Date32Array&>(*chunk);
        for (int64_t i = 0; i < a.length(); ++i) out.push_back(a.IsNull(i) ? 0 : a.Value(i));
    }
    return out;
}
inline std::vector<int32_t> as_date_days(const arrow::Table& t, const std::string& name) { return as_date_days(column(t, name)); }

}  // namespace ingest
}  // namespace synnodb
