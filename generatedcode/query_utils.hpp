#pragma once
// query_utils.hpp — Shared utilities for BFF query implementations.
//
// Provides column decoding, date conversion, decimal formatting, name
// reconstruction, and zone-map predicate evaluation helpers.
//
// FILE_VERSION: 1

#include "bff_format.hpp"
#include "read_api.hpp"

#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <iomanip>
#include <sstream>
#include <string>
#include <vector>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

// ============================================================
// Bit-unpack helper
// ============================================================

// Decode N bit-packed values (bit_width bits each, LSB-first) from src into out.
inline void bitunpack(const uint8_t* src, size_t n, uint8_t bits,
                      std::vector<int64_t>& out) {
    if (bits == 0 || n == 0) { out.assign(n, 0); return; }
    out.resize(n);
    uint64_t mask = (bits == 64) ? ~0ULL : ((1ULL << bits) - 1);
    uint64_t bit_buf = 0;
    int bit_pos = 0;
    size_t byte_pos = 0;
    for (size_t i = 0; i < n; i++) {
        while (bit_pos < bits) {
            bit_buf |= (uint64_t(src[byte_pos++]) << bit_pos);
            bit_pos += 8;
        }
        out[i] = int64_t(bit_buf & mask);
        bit_buf >>= bits;
        bit_pos -= bits;
    }
}

// ============================================================
// Column block decoder
// ============================================================

// Decode a single column block from raw bytes (block header + subheader + payload)
// into a vector of int64 values. For dict-encoded columns, returns the dict codes.
// For RAW encoding with phys_bytes < 8, sign-extends or zero-extends as needed.
// Returns false if the block could not be decoded.
inline bool decode_column_block(const uint8_t* block_data, size_t block_size,
                                 const CsfColMeta& meta,
                                 std::vector<int64_t>& out_values) {
    if (!block_data || block_size < sizeof(CsfBlockHeader)) return false;
    CsfBlockHeader hdr;
    memcpy(&hdr, block_data, sizeof(hdr));
    if (hdr.magic != CSF_BLOCK_MAGIC) return false;
    const uint8_t* after_hdr = block_data + sizeof(hdr);
    CsfEncoding enc = CsfEncoding(hdr.encoding);

    // --- RAW encoding ---
    if (enc == CsfEncoding::RAW) {
        // payload is at after_hdr (already decompressed = no compression in identity mode)
        // raw_bytes = num_values * phys_bytes
        uint32_t n = hdr.num_values;
        uint8_t pw = meta.phys_bytes;
        out_values.resize(n);
        const uint8_t* payload = after_hdr;
        // compressed_bytes == raw_bytes (identity compression)
        for (uint32_t i = 0; i < n; i++) {
            uint64_t v = 0;
            memcpy(&v, payload + i * pw, pw);
            out_values[i] = int64_t(v);
        }
        return true;
    }

    // --- FOR_BITPACK encoding ---
    if (enc == CsfEncoding::FOR_BITPACK) {
        CsfForHeader fh;
        memcpy(&fh, after_hdr, sizeof(fh));
        const uint8_t* payload = after_hdr + sizeof(fh);
        // payload is bit-packed, raw_bytes total
        std::vector<int64_t> packed;
        bitunpack(payload, hdr.num_values, hdr.bit_width, packed);
        out_values.resize(hdr.num_values);
        for (uint32_t i = 0; i < hdr.num_values; i++)
            out_values[i] = fh.base + packed[i];
        return true;
    }

    // --- DELTA_BITPACK encoding ---
    if (enc == CsfEncoding::DELTA_BITPACK) {
        CsfDeltaHeader dh;
        memcpy(&dh, after_hdr, sizeof(dh));
        const uint8_t* payload = after_hdr + sizeof(dh);
        std::vector<int64_t> deltas;
        bitunpack(payload, hdr.num_values, hdr.bit_width, deltas);
        out_values.resize(hdr.num_values);
        int64_t cur = dh.first;
        for (uint32_t i = 0; i < hdr.num_values; i++) {
            // Deltas are stored as unsigned; sign-extend if needed.
            // For date columns sorted ascending, deltas are small non-negative.
            // For occasional negative deltas, the bit pattern wraps.
            // Since we only use DELTA_BITPACK for ascending-sorted date columns,
            // deltas should be non-negative after sort. Just add.
            if (i == 0) { out_values[0] = dh.first; cur = dh.first; }
            else { cur += deltas[i]; out_values[i] = cur; }
        }
        return true;
    }

    // --- DICT_BITPACK encoding ---
    if (enc == CsfEncoding::DICT_BITPACK) {
        CsfDictHeader dicth;
        memcpy(&dicth, after_hdr, sizeof(dicth));
        const uint8_t* payload = after_hdr + sizeof(dicth);
        bitunpack(payload, hdr.num_values, hdr.bit_width, out_values);
        return true;
    }

    // --- RLE encoding ---
    if (enc == CsfEncoding::RLE) {
        const uint8_t* payload = after_hdr;
        // payload: (value uint64, count uint32) pairs
        out_values.clear();
        out_values.reserve(hdr.num_values);
        size_t offset = 0;
        while (out_values.size() < hdr.num_values && offset + 12 <= block_size - sizeof(hdr)) {
            uint64_t val; uint32_t cnt;
            memcpy(&val, payload + offset, 8); offset += 8;
            memcpy(&cnt, payload + offset, 4); offset += 4;
            for (uint32_t i = 0; i < cnt && out_values.size() < hdr.num_values; i++)
                out_values.push_back(int64_t(val));
        }
        return true;
    }

    // STRING_RAW: use decode_string_block instead
    return false;
}

// Decode a string block into a vector of strings.
inline bool decode_string_block(const uint8_t* block_data, size_t block_size,
                                 std::vector<std::string>& out_strings) {
    if (!block_data || block_size < sizeof(CsfBlockHeader)) return false;
    CsfBlockHeader hdr;
    memcpy(&hdr, block_data, sizeof(hdr));
    if (hdr.magic != CSF_BLOCK_MAGIC) return false;
    if (CsfEncoding(hdr.encoding) != CsfEncoding::STRING_RAW) return false;

    // After block header comes CsfStringHeader, then offsets, then payload
    const uint8_t* after_hdr = block_data + sizeof(hdr);
    // In identity-compressed mode, compressed == raw. The string header is
    // followed by the offset bytes then the payload bytes.
    // We skip the CsfStringHeader (it's only needed for LZ4 decompression).
    const uint8_t* payload_start = after_hdr + sizeof(CsfStringHeader);

    uint32_t n = hdr.num_values;
    // offset_compressed_bytes / payload_compressed_bytes are equal to raw sizes
    CsfStringHeader sh;
    memcpy(&sh, after_hdr, sizeof(sh));

    // Offset array: (n+1) uint32 values
    uint32_t off_raw_bytes = (n + 1) * sizeof(uint32_t);
    // offset data starts right after the string header
    const uint32_t* offsets = reinterpret_cast<const uint32_t*>(payload_start);
    // payload data follows the offset array
    const uint8_t* str_payload = payload_start + off_raw_bytes;

    out_strings.resize(n);
    for (uint32_t i = 0; i < n; i++) {
        uint32_t start = offsets[i];
        uint32_t end   = offsets[i+1];
        if (end >= start)
            out_strings[i].assign(reinterpret_cast<const char*>(str_payload + start), end - start);
        else
            out_strings[i].clear();
    }
    return true;
}

// ============================================================
// Read a column block from the .csf file
// Returns newly allocated buffer; caller calls release_bff_buffer().
// ============================================================
inline BffBuffer* read_col_block(BffTable* tbl, uint32_t seg, uint32_t col_id) {
    BffReadOptions opts;
    opts.decompress = false; // identity compression: raw == compressed
    return read_bff_page(tbl, seg, col_id, 0, opts);
}

// Decode int64 values from a BffBuffer (read_col_block result).
// Returns true on success.
inline bool decode_int_block(const BffBuffer* buf, const CsfColMeta& meta,
                              std::vector<int64_t>& out) {
    if (!buf || !buf->data || buf->size == 0) return false;
    return decode_column_block(buf->data, buf->size, meta, out);
}

inline bool decode_str_block(const BffBuffer* buf, std::vector<std::string>& out) {
    if (!buf || !buf->data || buf->size == 0) return false;
    return decode_string_block(buf->data, buf->size, out);
}

// ============================================================
// Column ID lookup helpers
// ============================================================

inline uint32_t find_col_id(const CsfTableFooter& ft, const std::string& name) {
    for (uint32_t i = 0; i < ft.num_cols; i++)
        if (ft.cols[i].name == name) return i;
    return UINT32_MAX;
}

// ============================================================
// Date helpers
// ============================================================

// Convert CSF epoch days (since 1992-01-01) to a "YYYY-MM-DD" string.
inline std::string csf_date_to_string(int32_t csf_days) {
    // Convert to Unix days (since 1970-01-01)
    int32_t unix_days = csf_days + CSF_DATE_EPOCH_OFFSET;
    // Convert to y/m/d using the algorithm from chrono
    int z = unix_days + 719468;
    int era = (z >= 0 ? z : z - 146096) / 146097;
    int doe = z - era * 146097;
    int yoe = (doe - doe/1460 + doe/36524 - doe/146096) / 365;
    int y = yoe + era * 400;
    int doy = doe - (365*yoe + yoe/4 - yoe/100);
    int mp = (5*doy + 2)/153;
    int d = doy - (153*mp+2)/5 + 1;
    int m = mp < 10 ? mp+3 : mp-9;
    y += (m <= 2);
    char buf[12];
    snprintf(buf, sizeof(buf), "%04d-%02d-%02d", y, m, d);
    return buf;
}

// Parse "YYYY-MM-DD" to Unix days (since 1970-01-01)
inline int32_t parse_date_to_unix_days(const std::string& date_str) {
    int y = std::stoi(date_str.substr(0,4));
    int m = std::stoi(date_str.substr(5,2));
    int d = std::stoi(date_str.substr(8,2));
    // days_from_civil (Howard Hinnant's algorithm)
    y -= (m <= 2);
    int era = (y >= 0 ? y : y - 399) / 400;
    int yoe = y - era * 400;
    int doy = (153*(m > 2 ? m-3 : m+9) + 2)/5 + d - 1;
    int doe = yoe*365 + yoe/4 - yoe/100 + doy;
    return era * 146097 + doe - 719468;
}

// Parse "YYYY-MM-DD" to CSF epoch days
inline int32_t parse_date_to_csf_days(const std::string& date_str) {
    return parse_date_to_unix_days(date_str) - CSF_DATE_EPOCH_OFFSET;
}

// Add N months to a date string, return new date string
inline std::string add_months_to_date(const std::string& date_str, int months) {
    int y = std::stoi(date_str.substr(0,4));
    int m = std::stoi(date_str.substr(5,2));
    int d = std::stoi(date_str.substr(8,2));
    m += months;
    while (m > 12) { m -= 12; y++; }
    while (m < 1)  { m += 12; y--; }
    // Clamp day to month end
    static const int days_in_month[] = {0,31,28,31,30,31,30,31,31,30,31,30,31};
    auto is_leap = [](int yr) { return (yr%4==0 && yr%100!=0) || yr%400==0; };
    int max_d = days_in_month[m] + (m==2 && is_leap(y) ? 1 : 0);
    if (d > max_d) d = max_d;
    char buf[12];
    snprintf(buf, sizeof(buf), "%04d-%02d-%02d", y, m, d);
    return buf;
}

// ============================================================
// Decimal formatting
// ============================================================

// Format a scaled integer as a decimal string with 2 decimal places.
// stored_val is the raw Arrow value; scale = 100 for 2 decimal places.
inline std::string format_decimal2(int64_t stored_val, int32_t scale = 100) {
    // stored_val / scale = decimal value
    int64_t integer_part = stored_val / scale;
    int64_t frac_part    = std::abs(stored_val % scale);
    if (stored_val < 0 && integer_part == 0)
        return "-" + std::to_string(integer_part) + "." + 
               std::string(2 - std::to_string(frac_part).size(), '0') + 
               std::to_string(frac_part);
    char buf[32];
    snprintf(buf, sizeof(buf), "%lld.%02lld", (long long)integer_part, (long long)frac_part);
    return buf;
}

// ============================================================
// Name reconstruction
// ============================================================

inline std::string format_customer_name(uint32_t suffix) {
    char buf[32];
    snprintf(buf, sizeof(buf), "Customer#%09u", suffix);
    return buf;
}

inline std::string format_supplier_name(uint32_t suffix) {
    char buf[32];
    snprintf(buf, sizeof(buf), "Supplier#%09u", suffix);
    return buf;
}

// ============================================================
// Phone reconstruction
// ============================================================

// Reconstruct "CC-NNN-NNN-NNNN" from cc (uint8) and rest (uint64).
// "rest" encodes the 10 remaining digits as a decimal integer.
inline std::string format_phone(uint8_t cc, uint64_t rest) {
    // rest = NNN*NNN*NNNN (10 digits grouped as 3-3-4)
    uint64_t last4  = rest % 10000;     rest /= 10000;
    uint64_t mid3   = rest % 1000;      rest /= 1000;
    uint64_t first3 = rest % 1000;
    char buf[20];
    snprintf(buf, sizeof(buf), "%02u-%03llu-%03llu-%04llu",
             (unsigned)cc, (unsigned long long)first3,
             (unsigned long long)mid3, (unsigned long long)last4);
    return buf;
}

// ============================================================
// Segment row count helper
// ============================================================
inline uint32_t seg_row_count(const CsfTableFooter& ft, uint32_t seg) {
    if (seg + 1 < ft.num_segments) return CSF_SEGMENT_ROWS;
    return uint32_t(ft.num_rows - uint64_t(seg) * CSF_SEGMENT_ROWS);
}