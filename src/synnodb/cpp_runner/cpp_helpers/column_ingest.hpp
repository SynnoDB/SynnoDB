#pragma once
// column_ingest.hpp - read Arrow columns into typed C++ vectors.
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
// NULLs. By default a null reads as the type's zero (0 / 0.0 / "" / epoch) - correct for a
// NOT NULL column and for SUM over a nullable one (adding zero == skipping). For a query whose
// result depends on SQL null semantics (COUNT(col), AVG, NULL-propagating arithmetic, a
// predicate on a nullable column, IS NULL), pass an optional ``Validity* valid_out``: it is
// filled with Arrow's valid_bytes (valid[i]==0 marks row i NULL), the symmetric counterpart to
// column_egress.hpp's Validity, so the engine can carry nulls through and re-emit them exactly.
//
// db_loader.cpp build() calls these and does not touch Arrow itself:
//   using namespace synnodb::ingest;
//   db->l_quantity      = scaled_int64(*tables->lineitem, "l_quantity", 2); // fixed-point
//   db->l_orderkey      = as_int64    (*tables->lineitem, "l_orderkey");
//   db->l_returnflag    = as_string   (*tables->lineitem, "l_returnflag");
//   db->l_shipdate_days = as_date_days(*tables->lineitem, "l_shipdate");
//   // nullable column: capture validity to honour SQL null semantics
//   Validity disc_valid;
//   db->o_discount      = as_double   (*tables->orders, "o_discount", &disc_valid);
//
// Tests: tests/test_column_ingest.py (TPC-H parquet vs DuckDB),
// tests/cpp/column_ingest_test.cpp (decimal/bool/dictionary/timestamp/nullable).

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

// Per-row validity, Arrow's valid_bytes convention: valid[i]==0 marks row i NULL. The same type
// and convention as synnodb::egress::Validity, so a mask read on the way in can be carried
// straight back out. An optional ``Validity*`` out-parameter on each reader fills it; passing
// nullptr (the default) keeps the historical null-reads-as-zero behaviour.
using Validity = std::vector<uint8_t>;

namespace detail {
inline void init_validity(Validity* valid_out, int64_t n) {
    if (valid_out) { valid_out->clear(); valid_out->reserve(static_cast<size_t>(n)); }
}
inline void push_validity(Validity* valid_out, bool is_null) {
    if (valid_out) valid_out->push_back(is_null ? 0 : 1);
}
}  // namespace detail

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
inline std::vector<int64_t> as_int64(const std::shared_ptr<arrow::ChunkedArray>& col,
                                     Validity* valid_out = nullptr) {
    auto c = canonicalize(col, arrow::int64());
    std::vector<int64_t> out;
    out.reserve(static_cast<size_t>(c->length()));
    detail::init_validity(valid_out, c->length());
    for (const auto& chunk : c->chunks()) {
        const auto& a = static_cast<const arrow::Int64Array&>(*chunk);
        for (int64_t i = 0; i < a.length(); ++i) {
            const bool null = a.IsNull(i);
            detail::push_validity(valid_out, null);
            out.push_back(null ? 0 : a.Value(i));
        }
    }
    return out;
}
inline std::vector<int64_t> as_int64(const arrow::Table& t, const std::string& name,
                                     Validity* valid_out = nullptr) {
    return as_int64(column(t, name), valid_out);
}

// ---- as_double: any numeric/decimal source -> double -------------------------
inline std::vector<double> as_double(const std::shared_ptr<arrow::ChunkedArray>& col,
                                     Validity* valid_out = nullptr) {
    auto c = canonicalize(col, arrow::float64());
    std::vector<double> out;
    out.reserve(static_cast<size_t>(c->length()));
    detail::init_validity(valid_out, c->length());
    for (const auto& chunk : c->chunks()) {
        const auto& a = static_cast<const arrow::DoubleArray&>(*chunk);
        for (int64_t i = 0; i < a.length(); ++i) {
            const bool null = a.IsNull(i);
            detail::push_validity(valid_out, null);
            out.push_back(null ? 0.0 : a.Value(i));
        }
    }
    return out;
}
inline std::vector<double> as_double(const arrow::Table& t, const std::string& name,
                                     Validity* valid_out = nullptr) {
    return as_double(column(t, name), valid_out);
}

// ---- scaled_int64: numeric/decimal source -> fixed-point int64 (value*10^scale) -----
// Read each value's unscaled integer at the requested scale. A value that does not fit int64 at
// this scale throws rather than truncating.
//
// Fast path: a decimal128 source ALREADY at `scale` is read in place - its unscaled int64 is
// already value*10^scale, so casting it to decimal128(38,scale) would only copy 16 bytes/row
// (~1.9 GB per TPC-H lineitem column at SF20) without changing a single value. Skipping that cast
// makes the helper as cheap as a hand-rolled raw read, so there is no performance reason to decode
// Arrow by hand (and lose the overflow check, null handling, and type-universality below). Any
// other source (different scale, integer, float, decimal256) still goes through Cast so the helper
// stays universal and exact.
inline std::vector<int64_t> scaled_int64(const std::shared_ptr<arrow::ChunkedArray>& col, int scale,
                                         Validity* valid_out = nullptr) {
    std::shared_ptr<arrow::ChunkedArray> c;
    if (col->type()->id() == arrow::Type::DECIMAL128 &&
        static_cast<const arrow::Decimal128Type&>(*col->type()).scale() == scale) {
        c = col;  // native scale: no widening Cast, read the unscaled int64 directly
    } else {
        c = canonicalize(col, arrow::decimal128(38, scale));
    }
    std::vector<int64_t> out;
    out.reserve(static_cast<size_t>(c->length()));
    detail::init_validity(valid_out, c->length());
    for (const auto& chunk : c->chunks()) {
        const auto& a = static_cast<const arrow::Decimal128Array&>(*chunk);
        for (int64_t i = 0; i < a.length(); ++i) {
            if (a.IsNull(i)) { detail::push_validity(valid_out, true); out.push_back(0); continue; }
            detail::push_validity(valid_out, false);
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
inline std::vector<int64_t> scaled_int64(const arrow::Table& t, const std::string& name, int scale,
                                         Validity* valid_out = nullptr) {
    return scaled_int64(column(t, name), scale, valid_out);
}

// ---- as_string: any string/dictionary/etc. source -> std::string -------------
inline std::vector<std::string> as_string(const std::shared_ptr<arrow::ChunkedArray>& col,
                                          Validity* valid_out = nullptr) {
    auto c = canonicalize(col, arrow::utf8());
    std::vector<std::string> out;
    out.reserve(static_cast<size_t>(c->length()));
    detail::init_validity(valid_out, c->length());
    for (const auto& chunk : c->chunks()) {
        const auto& a = static_cast<const arrow::StringArray&>(*chunk);
        for (int64_t i = 0; i < a.length(); ++i) {
            const bool null = a.IsNull(i);
            detail::push_validity(valid_out, null);
            out.emplace_back(null ? std::string() : a.GetString(i));
        }
    }
    return out;
}
inline std::vector<std::string> as_string(const arrow::Table& t, const std::string& name,
                                          Validity* valid_out = nullptr) {
    return as_string(column(t, name), valid_out);
}

// ---- as_date_days: any date/timestamp source -> int32 days since 1970-01-01 ---
inline std::vector<int32_t> as_date_days(const std::shared_ptr<arrow::ChunkedArray>& col,
                                         Validity* valid_out = nullptr) {
    auto c = canonicalize(col, arrow::date32());
    std::vector<int32_t> out;
    out.reserve(static_cast<size_t>(c->length()));
    detail::init_validity(valid_out, c->length());
    for (const auto& chunk : c->chunks()) {
        const auto& a = static_cast<const arrow::Date32Array&>(*chunk);
        for (int64_t i = 0; i < a.length(); ++i) {
            const bool null = a.IsNull(i);
            detail::push_validity(valid_out, null);
            out.push_back(null ? 0 : a.Value(i));
        }
    }
    return out;
}
inline std::vector<int32_t> as_date_days(const arrow::Table& t, const std::string& name,
                                         Validity* valid_out = nullptr) {
    return as_date_days(column(t, name), valid_out);
}

}  // namespace ingest
}  // namespace synnodb
