#include "write_api.hpp"
#include "bff_format.hpp"
#include "parquet_reader.hpp"

#include <arrow/api.h>
#include <arrow/array/array_primitive.h>
#include <arrow/array/array_binary.h>
#include <arrow/array/array_decimal.h>
#include <arrow/util/decimal.h>
#include <arrow/type.h>
#include <arrow/type_traits.h>

#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <limits>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

#include <fcntl.h>
#include <unistd.h>

// FILE_VERSION: 2

// ============================================================
// Arrow value extraction helpers (runtime type dispatch)
// ============================================================

static int64_t arrow_get_int64(const arrow::Array* arr, int64_t i) {
    using namespace arrow;
    auto tid = arr->type_id();
    switch (tid) {
        case Type::INT8:   return static_cast<const NumericArray<Int8Type>*>(arr)->Value(i);
        case Type::INT16:  return static_cast<const NumericArray<Int16Type>*>(arr)->Value(i);
        case Type::INT32:  return static_cast<const NumericArray<Int32Type>*>(arr)->Value(i);
        case Type::INT64:  return static_cast<const NumericArray<Int64Type>*>(arr)->Value(i);
        case Type::UINT8:  return static_cast<int64_t>(static_cast<const NumericArray<UInt8Type>*>(arr)->Value(i));
        case Type::UINT16: return static_cast<int64_t>(static_cast<const NumericArray<UInt16Type>*>(arr)->Value(i));
        case Type::UINT32: return static_cast<int64_t>(static_cast<const NumericArray<UInt32Type>*>(arr)->Value(i));
        case Type::UINT64: return static_cast<int64_t>(static_cast<const NumericArray<UInt64Type>*>(arr)->Value(i));
        case Type::FLOAT:  return static_cast<int64_t>(std::llround(static_cast<const NumericArray<FloatType>*>(arr)->Value(i)));
        case Type::DOUBLE: return static_cast<int64_t>(std::llround(static_cast<const NumericArray<DoubleType>*>(arr)->Value(i)));
        case Type::DATE32: return static_cast<const NumericArray<Date32Type>*>(arr)->Value(i);
        case Type::DECIMAL128: {
            const auto* da = static_cast<const Decimal128Array*>(arr);
            Decimal128 d(da->GetValue(i));
            return static_cast<int64_t>(d);
        }
        default:
            return 0;
    }
}

static std::string_view arrow_get_string(const arrow::Array* arr, int64_t i) {
    using namespace arrow;
    auto tid = arr->type_id();
    if (tid == Type::STRING || tid == Type::BINARY) {
        const auto* sa = static_cast<const StringArray*>(arr);
        return sa->GetView(i);
    }
    if (tid == Type::LARGE_STRING || tid == Type::LARGE_BINARY) {
        const auto* sa = static_cast<const LargeStringArray*>(arr);
        return sa->GetView(i);
    }
    return {};
}

// ============================================================
// Bit-packing (LSB-first within each byte)
// ============================================================

static void bitpack_encode(const std::vector<uint64_t>& vals, uint8_t bits,
                           std::vector<uint8_t>& out) {
    if (bits == 0 || vals.empty()) return;
    size_t n = vals.size();
    size_t total_bits = (size_t)n * bits;
    size_t total_bytes = (total_bits + 7) / 8;
    size_t base = out.size();
    out.resize(base + total_bytes, 0);
    uint64_t bit_buf = 0;
    int bit_pos = 0;
    size_t byte_pos = base;
    for (size_t i = 0; i < n; i++) {
        bit_buf |= (vals[i] << bit_pos);
        bit_pos += bits;
        while (bit_pos >= 8) {
            out[byte_pos++] = uint8_t(bit_buf & 0xFF);
            bit_buf >>= 8;
            bit_pos -= 8;
        }
    }
    if (bit_pos > 0) out[byte_pos] = uint8_t(bit_buf & 0xFF);
}

// ============================================================
// Buffered file writer (avoids many small syscalls)
// ============================================================
struct FileWriter {
    static constexpr size_t BUF_SIZE = 1 << 22; // 4 MB
    int fd = -1;
    uint8_t* buf = nullptr;
    size_t buf_used = 0;
    size_t file_committed = 0;

    void open(const std::string& path) {
        fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (fd < 0) throw std::runtime_error("Cannot open for write: " + path);
        buf = new uint8_t[BUF_SIZE];
        buf_used = 0;
        file_committed = 0;
    }
    size_t tell() const { return file_committed + buf_used; }
    void flush_buf() {
        if (buf_used == 0) return;
        ssize_t wr = ::write(fd, buf, buf_used);
        (void)wr;
        file_committed += buf_used;
        buf_used = 0;
    }
    void write_bytes(const void* data, size_t len) {
        const uint8_t* src = static_cast<const uint8_t*>(data);
        while (len > 0) {
            size_t space = BUF_SIZE - buf_used;
            if (space == 0) { flush_buf(); space = BUF_SIZE; }
            size_t n = std::min(len, space);
            memcpy(buf + buf_used, src, n);
            buf_used += n; src += n; len -= n;
        }
    }
    void pwrite_at(const void* data, size_t len, size_t offset) {
        flush_buf();
        ::pwrite(fd, data, len, (off_t)offset);
    }
    void close() {
        flush_buf();
        ::close(fd);
        fd = -1;
        delete[] buf; buf = nullptr;
    }
};

// ============================================================
// Column block writers — write into a reusable output vector
// ============================================================

static uint8_t bits_needed(uint64_t max_val) {
    if (max_val == 0) return 1;
    uint8_t b = 0;
    while ((1ULL << b) <= max_val) b++;
    return b;
}

static void write_for_bitpack_block(const std::vector<int64_t>& values,
                                     std::vector<uint8_t>& out,
                                     CsfBlockHeader& hdr_out,
                                     int64_t& base_out) {
    if (values.empty()) { hdr_out = {}; base_out = 0; return; }
    int64_t vmin = values[0], vmax = values[0];
    for (auto v : values) { if (v < vmin) vmin = v; if (v > vmax) vmax = v; }
    base_out = vmin;
    uint64_t range = uint64_t(vmax - vmin);
    uint8_t bits = bits_needed(range);
    size_t n = values.size();
    size_t raw_bytes_count = (n * bits + 7) / 8;

    CsfForHeader fh; fh.base = vmin;
    CsfBlockHeader hdr;
    hdr.magic            = CSF_BLOCK_MAGIC;
    hdr.encoding         = uint8_t(CsfEncoding::FOR_BITPACK);
    hdr.bit_width        = bits;
    hdr.compressed_bytes = uint32_t(raw_bytes_count);
    hdr.raw_bytes        = uint32_t(raw_bytes_count);
    hdr.num_values       = uint32_t(n);
    hdr_out = hdr;

    size_t old = out.size();
    out.resize(old + sizeof(hdr) + sizeof(fh) + raw_bytes_count, 0);
    memcpy(out.data() + old, &hdr, sizeof(hdr));
    memcpy(out.data() + old + sizeof(hdr), &fh, sizeof(fh));
    uint8_t* dst = out.data() + old + sizeof(hdr) + sizeof(fh);
    uint64_t bit_buf = 0; int bit_pos = 0; size_t byte_pos = 0;
    for (size_t i = 0; i < n; i++) {
        bit_buf |= (uint64_t(values[i] - vmin) << bit_pos);
        bit_pos += bits;
        while (bit_pos >= 8) { dst[byte_pos++] = uint8_t(bit_buf); bit_buf >>= 8; bit_pos -= 8; }
    }
    if (bit_pos > 0) dst[byte_pos] = uint8_t(bit_buf);
}

static void write_delta_bitpack_block(const std::vector<int64_t>& values,
                                       std::vector<uint8_t>& out,
                                       CsfBlockHeader& hdr_out,
                                       int64_t& first_out) {
    if (values.empty()) { hdr_out = {}; first_out = 0; return; }
    first_out = values[0];
    int64_t max_delta = 0;
    for (size_t i = 1; i < values.size(); i++) {
        int64_t d = values[i] - values[i-1];
        if (d < 0) d = -d;
        if (d > max_delta) max_delta = d;
    }
    uint8_t bits = bits_needed(uint64_t(max_delta));
    size_t n = values.size();
    size_t raw_bytes_count = (n * bits + 7) / 8;

    CsfDeltaHeader dh; dh.first = first_out;
    CsfBlockHeader hdr;
    hdr.magic            = CSF_BLOCK_MAGIC;
    hdr.encoding         = uint8_t(CsfEncoding::DELTA_BITPACK);
    hdr.bit_width        = bits;
    hdr.compressed_bytes = uint32_t(raw_bytes_count);
    hdr.raw_bytes        = uint32_t(raw_bytes_count);
    hdr.num_values       = uint32_t(n);
    hdr_out = hdr;

    size_t old = out.size();
    out.resize(old + sizeof(hdr) + sizeof(dh) + raw_bytes_count, 0);
    memcpy(out.data() + old, &hdr, sizeof(hdr));
    memcpy(out.data() + old + sizeof(hdr), &dh, sizeof(dh));
    uint8_t* dst = out.data() + old + sizeof(hdr) + sizeof(dh);
    uint64_t bit_buf = 0; int bit_pos = 0; size_t byte_pos = 0;
    for (size_t i = 0; i < n; i++) {
        uint64_t d = (i == 0) ? 0ULL : uint64_t(values[i] - values[i-1]);
        bit_buf |= (d << bit_pos);
        bit_pos += bits;
        while (bit_pos >= 8) { dst[byte_pos++] = uint8_t(bit_buf); bit_buf >>= 8; bit_pos -= 8; }
    }
    if (bit_pos > 0) dst[byte_pos] = uint8_t(bit_buf);
}

static void write_dict_bitpack_block(const std::vector<uint64_t>& codes,
                                      uint8_t bits, uint16_t dict_id,
                                      std::vector<uint8_t>& out,
                                      CsfBlockHeader& hdr_out) {
    size_t n = codes.size();
    size_t raw_bytes_count = (n * bits + 7) / 8;

    CsfDictHeader dh; dh.dict_id = dict_id;
    CsfBlockHeader hdr;
    hdr.magic            = CSF_BLOCK_MAGIC;
    hdr.encoding         = uint8_t(CsfEncoding::DICT_BITPACK);
    hdr.bit_width        = bits;
    hdr.compressed_bytes = uint32_t(raw_bytes_count);
    hdr.raw_bytes        = uint32_t(raw_bytes_count);
    hdr.num_values       = uint32_t(n);
    hdr_out = hdr;

    size_t old = out.size();
    out.resize(old + sizeof(hdr) + sizeof(dh) + raw_bytes_count, 0);
    memcpy(out.data() + old, &hdr, sizeof(hdr));
    memcpy(out.data() + old + sizeof(hdr), &dh, sizeof(dh));
    uint8_t* dst = out.data() + old + sizeof(hdr) + sizeof(dh);
    uint64_t bit_buf = 0; int bit_pos = 0; size_t byte_pos = 0;
    for (size_t i = 0; i < n; i++) {
        bit_buf |= (codes[i] << bit_pos);
        bit_pos += bits;
        while (bit_pos >= 8) { dst[byte_pos++] = uint8_t(bit_buf); bit_buf >>= 8; bit_pos -= 8; }
    }
    if (bit_pos > 0) dst[byte_pos] = uint8_t(bit_buf);
}

static void write_raw_block(const std::vector<int64_t>& values,
                             uint8_t phys_bytes,
                             std::vector<uint8_t>& out,
                             CsfBlockHeader& hdr_out) {
    uint32_t raw_size = uint32_t(values.size() * phys_bytes);
    CsfBlockHeader hdr;
    hdr.magic            = CSF_BLOCK_MAGIC;
    hdr.encoding         = uint8_t(CsfEncoding::RAW);
    hdr.bit_width        = 0;
    hdr.compressed_bytes = raw_size;
    hdr.raw_bytes        = raw_size;
    hdr.num_values       = uint32_t(values.size());
    hdr_out = hdr;
    size_t old = out.size();
    out.resize(old + sizeof(hdr) + raw_size);
    memcpy(out.data() + old, &hdr, sizeof(hdr));
    uint8_t* dst = out.data() + old + sizeof(hdr);
    for (size_t i = 0; i < values.size(); i++) {
        uint64_t uv = uint64_t(values[i]);
        memcpy(dst + i * phys_bytes, &uv, phys_bytes);
    }
}

static void write_rle_block(int64_t value, uint32_t count,
                             std::vector<uint8_t>& out,
                             CsfBlockHeader& hdr_out) {
    CsfBlockHeader hdr;
    hdr.magic            = CSF_BLOCK_MAGIC;
    hdr.encoding         = uint8_t(CsfEncoding::RLE);
    hdr.bit_width        = 0;
    hdr.compressed_bytes = 12;
    hdr.raw_bytes        = 12;
    hdr.num_values       = count;
    hdr_out = hdr;
    size_t old = out.size();
    out.resize(old + sizeof(hdr) + 12);
    memcpy(out.data() + old, &hdr, sizeof(hdr));
    uint64_t uv = uint64_t(value);
    memcpy(out.data() + old + sizeof(hdr), &uv, 8);
    memcpy(out.data() + old + sizeof(hdr) + 8, &count, 4);
}

static void write_string_block(const std::vector<std::string_view>& strs,
                                std::vector<uint8_t>& out,
                                CsfBlockHeader& hdr_out) {
    uint32_t n = uint32_t(strs.size());
    std::vector<uint32_t> offsets(n + 1);
    uint32_t payload_size = 0;
    for (uint32_t i = 0; i < n; i++) {
        offsets[i] = payload_size;
        payload_size += uint32_t(strs[i].size());
    }
    offsets[n] = payload_size;

    uint32_t off_raw = uint32_t((n+1) * sizeof(uint32_t));
    CsfStringHeader sh;
    sh.offset_compressed_bytes  = off_raw;
    sh.payload_compressed_bytes = payload_size;
    uint32_t total = off_raw + payload_size;

    CsfBlockHeader hdr;
    hdr.magic            = CSF_BLOCK_MAGIC;
    hdr.encoding         = uint8_t(CsfEncoding::STRING_RAW);
    hdr.bit_width        = 0;
    hdr.compressed_bytes = total;
    hdr.raw_bytes        = total;
    hdr.num_values       = n;
    hdr_out = hdr;

    size_t old = out.size();
    out.resize(old + sizeof(hdr) + sizeof(sh) + total);
    uint8_t* dst = out.data() + old;
    memcpy(dst, &hdr, sizeof(hdr)); dst += sizeof(hdr);
    memcpy(dst, &sh, sizeof(sh));  dst += sizeof(sh);
    memcpy(dst, offsets.data(), off_raw); dst += off_raw;
    for (uint32_t i = 0; i < n; i++) {
        if (!strs[i].empty())
            memcpy(dst + offsets[i], strs[i].data(), strs[i].size());
    }
}

// ============================================================
// Footer serialization
// ============================================================

static std::vector<uint8_t> serialize_table_footer(const CsfTableFooter& ft) {
    std::vector<uint8_t> buf;

    std::vector<uint8_t> schema_buf;
    ByteWriter sw(schema_buf);
    sw.write_u32(ft.num_cols);
    for (const auto& c : ft.cols) {
        sw.write_str(c.name);
        sw.write_u8(uint8_t(c.encoding));
        sw.write_u8(c.phys_bytes);
        sw.write_u8(c.bit_width);
        sw.write_u8(c.dict_id);
        sw.write_u8(c.is_signed ? 1 : 0);
        sw.write_u32(uint32_t(c.scale));
        sw.write_u32(uint32_t(c.date_epoch));
        sw.write_u8(c.nullable ? 1 : 0);
        sw.write_u8(c.synthetic_prefix ? 1 : 0);
        sw.write_str(c.prefix_str);
        sw.write_u8(c.split_phone ? 1 : 0);
        sw.write_u8(uint8_t(c.bff_phys));
    }

    std::vector<uint8_t> dict_buf;
    ByteWriter dw(dict_buf);
    dw.write_u32(uint32_t(ft.dicts.size()));
    for (const auto& d : ft.dicts) {
        dw.write_u8(d.dict_id);
        dw.write_str(d.col_name);
        dw.write_u32(uint32_t(d.entries.size()));
        for (const auto& e : d.entries) dw.write_str(e);
    }

    std::vector<uint8_t> seg_buf;
    ByteWriter segw(seg_buf);
    segw.write_u64(ft.num_rows);
    segw.write_u32(ft.num_segments);
    segw.write_u32(ft.num_cols);
    for (uint32_t s = 0; s < ft.num_segments; s++) {
        segw.write_u64(ft.seg_file_offsets[s]);
        for (uint32_t c = 0; c < ft.num_cols; c++)
            segw.write_u64(ft.col_block_offsets[s * ft.num_cols + c]);
    }

    std::vector<uint8_t> zm_buf;
    ByteWriter zw(zm_buf);
    for (uint32_t s = 0; s < ft.num_segments; s++) {
        const auto& zm = ft.zone_maps[s];
        for (uint32_t c = 0; c < ft.num_cols; c++) {
            zw.write_i64(zm.col_min[c]);
            zw.write_i64(zm.col_max[c]);
            zw.write_u64(zm.col_bitset[c]);
            zw.write_u64(zm.col_null_count[c]);
        }
    }

    std::vector<uint8_t> bloom_buf;
    ByteWriter blw(bloom_buf);
    for (uint32_t s = 0; s < ft.num_segments; s++) {
        const auto& zm = ft.zone_maps[s];
        blw.write_u32(uint32_t(zm.bloom.size()));
        blw.write_bytes(zm.bloom.data(), zm.bloom.size());
    }

    uint32_t toc_header_size = 4 + FOOTER_SECT_COUNT * 12;
    struct Sec { std::vector<uint8_t>* data; };
    Sec secs[FOOTER_SECT_COUNT] = {
        {&schema_buf}, {&dict_buf}, {&seg_buf}, {&zm_buf}, {&bloom_buf}
    };

    ByteWriter fw(buf);
    fw.write_u32(FOOTER_SECT_COUNT);
    uint32_t off = toc_header_size;
    for (int i = 0; i < FOOTER_SECT_COUNT; i++) {
        fw.write_u8(uint8_t(i));
        fw.write_u8(0); fw.write_u8(0); fw.write_u8(0);
        fw.write_u32(off);
        fw.write_u32(uint32_t(secs[i].data->size()));
        off += uint32_t(secs[i].data->size());
    }
    for (int i = 0; i < FOOTER_SECT_COUNT; i++)
        fw.write_bytes(secs[i].data->data(), secs[i].data->size());
    return buf;
}

// ============================================================
// Phone decomposition
// ============================================================

static std::pair<uint8_t, uint64_t> parse_phone(std::string_view phone) {
    uint8_t cc = 0;
    uint64_t rest = 0;
    if (phone.size() < 2) return {cc, rest};
    cc = uint8_t((phone[0]-'0')*10 + (phone[1]-'0'));
    uint64_t v = 0;
    for (size_t i = 3; i < phone.size(); i++) {
        char c = phone[i];
        if (c >= '0' && c <= '9') v = v * 10 + (c - '0');
    }
    rest = v;
    return {cc, rest};
}

// ============================================================
// Name suffix parsing
// ============================================================

static uint32_t parse_name_suffix(std::string_view name, size_t prefix_len) {
    if (name.size() <= prefix_len) return 0;
    uint32_t v = 0;
    for (size_t i = prefix_len; i < name.size(); i++) {
        char c = name[i];
        if (c >= '0' && c <= '9') v = v * 10 + uint32_t(c - '0');
    }
    return v;
}

// ============================================================
// Global dictionary builder
// ============================================================

static CsfDict build_dict(const arrow::Table* table, int col_idx,
                           uint8_t dict_id, const std::string& col_name) {
    CsfDict d;
    d.dict_id  = dict_id;
    d.col_name = col_name;
    std::unordered_map<std::string, uint32_t> seen;
    auto chunked = table->column(col_idx);
    for (int ci = 0; ci < chunked->num_chunks(); ci++) {
        auto chunk = chunked->chunk(ci).get();
        for (int64_t r = 0; r < chunk->length(); r++) {
            if (chunk->IsNull(r)) continue;
            std::string s(arrow_get_string(chunk, r));
            if (!seen.count(s)) {
                seen[s] = uint32_t(d.entries.size());
                d.entries.push_back(s);
            }
        }
    }
    std::sort(d.entries.begin(), d.entries.end());
    return d;
}

// ============================================================
// Fast chunk cache: special-cases single-chunk (common for Parquet)
// ============================================================
struct ChunkCache {
    struct ChunkEntry {
        const arrow::Array* arr;
        int64_t start;
    };
    std::vector<ChunkEntry> chunks;

    void init(const arrow::Table* t, int col) {
        auto chunked = t->column(col).get();
        int64_t off = 0;
        chunks.reserve(chunked->num_chunks());
        for (int ci = 0; ci < chunked->num_chunks(); ci++) {
            auto* a = chunked->chunk(ci).get();
            chunks.push_back({a, off});
            off += a->length();
        }
    }

    int find_chunk(int64_t row) const {
        int lo = 0, hi = int(chunks.size()) - 1;
        while (lo < hi) {
            int mid = (lo + hi + 1) / 2;
            if (chunks[mid].start <= row) lo = mid; else hi = mid - 1;
        }
        return lo;
    }

    int64_t get_int64(int64_t row) const {
        if (chunks.size() == 1) return arrow_get_int64(chunks[0].arr, row);
        int ci = find_chunk(row);
        return arrow_get_int64(chunks[ci].arr, row - chunks[ci].start);
    }
    std::string_view get_string(int64_t row) const {
        if (chunks.size() == 1) return arrow_get_string(chunks[0].arr, row);
        int ci = find_chunk(row);
        return arrow_get_string(chunks[ci].arr, row - chunks[ci].start);
    }
    bool is_null(int64_t row) const {
        if (chunks.size() == 1) return chunks[0].arr->IsNull(row);
        int ci = find_chunk(row);
        return chunks[ci].arr->IsNull(row - chunks[ci].start);
    }
};

// ============================================================
// ColSpec / TableSpec
// ============================================================

struct ColSpec {
    std::string  logical_name;
    int          arrow_col_idx;
    CsfEncoding  encoding;
    uint8_t      phys_bytes;
    uint8_t      dict_id;
    bool         is_date;
    int32_t      decimal_scale;
    bool         synthetic_pfx;
    std::string  prefix_str;
    bool         is_phone_cc;
    bool         is_phone_rest;
    bool         is_rle_const;
    int64_t      rle_value;
};

struct TableSpec {
    std::string          table_name;
    std::vector<ColSpec> cols;
    int                  sort_col_idx;
    bool                 sort_ascending;
    int                  sort_col2_idx;
    bool                 sort2_ascending;
};

// ============================================================
// Column index lookup
// ============================================================
static int find_col(const arrow::Table* table, const std::string& name) {
    return table->schema()->GetFieldIndex(name);
}

// ============================================================
// TableSpec builders
// ============================================================

static TableSpec make_lineitem_spec(const arrow::Table* table) {
    TableSpec ts;
    ts.table_name      = "lineitem";
    ts.sort_col_idx    = find_col(table, "l_shipdate");
    ts.sort_ascending  = true;
    ts.sort_col2_idx   = -1;
    ts.sort2_ascending = true;

    ts.cols.push_back({"l_orderkey",     find_col(table,"l_orderkey"),     CsfEncoding::FOR_BITPACK,   8, 0xFF, false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"l_partkey",      find_col(table,"l_partkey"),      CsfEncoding::FOR_BITPACK,   8, 0xFF, false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"l_suppkey",      find_col(table,"l_suppkey"),      CsfEncoding::FOR_BITPACK,   8, 0xFF, false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"l_linenumber",   find_col(table,"l_linenumber"),   CsfEncoding::RAW,           1, 0xFF, false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"l_quantity",     find_col(table,"l_quantity"),     CsfEncoding::RAW,           8, 0xFF, false, 100, false,"",false,false,false,0});
    ts.cols.push_back({"l_extendedprice",find_col(table,"l_extendedprice"),CsfEncoding::FOR_BITPACK,   8, 0xFF, false, 100, false,"",false,false,false,0});
    ts.cols.push_back({"l_discount",     find_col(table,"l_discount"),     CsfEncoding::RAW,           8, 0xFF, false, 100, false,"",false,false,false,0});
    ts.cols.push_back({"l_tax",          find_col(table,"l_tax"),          CsfEncoding::RAW,           8, 0xFF, false, 100, false,"",false,false,false,0});
    ts.cols.push_back({"l_returnflag",   find_col(table,"l_returnflag"),   CsfEncoding::DICT_BITPACK,  0, 0,    false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"l_linestatus",   find_col(table,"l_linestatus"),   CsfEncoding::DICT_BITPACK,  0, 1,    false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"l_shipdate",     find_col(table,"l_shipdate"),     CsfEncoding::DELTA_BITPACK, 8, 0xFF, true,  1,   false,"",false,false,false,0});
    ts.cols.push_back({"l_commitdate",   find_col(table,"l_commitdate"),   CsfEncoding::FOR_BITPACK,   8, 0xFF, true,  1,   false,"",false,false,false,0});
    ts.cols.push_back({"l_receiptdate",  find_col(table,"l_receiptdate"),  CsfEncoding::FOR_BITPACK,   8, 0xFF, true,  1,   false,"",false,false,false,0});
    ts.cols.push_back({"l_shipinstruct", find_col(table,"l_shipinstruct"), CsfEncoding::DICT_BITPACK,  0, 2,    false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"l_shipmode",     find_col(table,"l_shipmode"),     CsfEncoding::DICT_BITPACK,  0, 3,    false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"l_comment",      find_col(table,"l_comment"),      CsfEncoding::STRING_RAW,    0, 0xFF, false, 1,   false,"",false,false,false,0});
    return ts;
}

static TableSpec make_orders_spec(const arrow::Table* table) {
    TableSpec ts;
    ts.table_name      = "orders";
    ts.sort_col_idx    = find_col(table, "o_orderdate");
    ts.sort_ascending  = true;
    ts.sort_col2_idx   = -1;
    ts.sort2_ascending = true;

    ts.cols.push_back({"o_orderkey",      find_col(table,"o_orderkey"),     CsfEncoding::FOR_BITPACK,   8, 0xFF, false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"o_custkey",       find_col(table,"o_custkey"),      CsfEncoding::FOR_BITPACK,   8, 0xFF, false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"o_orderstatus",   find_col(table,"o_orderstatus"),  CsfEncoding::DICT_BITPACK,  0, 0,    false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"o_totalprice",    find_col(table,"o_totalprice"),   CsfEncoding::FOR_BITPACK,   8, 0xFF, false, 100, false,"",false,false,false,0});
    ts.cols.push_back({"o_orderdate",     find_col(table,"o_orderdate"),    CsfEncoding::DELTA_BITPACK, 8, 0xFF, true,  1,   false,"",false,false,false,0});
    ts.cols.push_back({"o_orderpriority", find_col(table,"o_orderpriority"),CsfEncoding::DICT_BITPACK,  0, 1,    false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"o_clerk",         find_col(table,"o_clerk"),        CsfEncoding::DICT_BITPACK,  0, 2,    false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"o_shippriority",  find_col(table,"o_shippriority"), CsfEncoding::RLE,           0, 0xFF, false, 1,   false,"",false,false,true, 0});
    ts.cols.push_back({"o_comment",       find_col(table,"o_comment"),      CsfEncoding::STRING_RAW,    0, 0xFF, false, 1,   false,"",false,false,false,0});
    return ts;
}

static TableSpec make_customer_spec(const arrow::Table* table) {
    TableSpec ts;
    ts.table_name      = "customer";
    ts.sort_col_idx    = -1;
    ts.sort_ascending  = true;
    ts.sort_col2_idx   = -1;
    ts.sort2_ascending = false;

    ts.cols.push_back({"c_custkey",    find_col(table,"c_custkey"),    CsfEncoding::FOR_BITPACK,  8, 0xFF, false, 1,   false,"",          false,false,false,0});
    ts.cols.push_back({"c_name",       find_col(table,"c_name"),       CsfEncoding::RAW,          4, 0xFF, false, 1,   true, "Customer#", false,false,false,0});
    ts.cols.push_back({"c_address",    find_col(table,"c_address"),    CsfEncoding::STRING_RAW,   0, 0xFF, false, 1,   false,"",          false,false,false,0});
    ts.cols.push_back({"c_nationkey",  find_col(table,"c_nationkey"),  CsfEncoding::RAW,          1, 0xFF, false, 1,   false,"",          false,false,false,0});
    ts.cols.push_back({"c_phone_cc",   find_col(table,"c_phone"),      CsfEncoding::RAW,          1, 0xFF, false, 1,   false,"",          true, false,false,0});
    ts.cols.push_back({"c_phone_rest", find_col(table,"c_phone"),      CsfEncoding::RAW,          8, 0xFF, false, 1,   false,"",          false,true, false,0});
    ts.cols.push_back({"c_acctbal",    find_col(table,"c_acctbal"),    CsfEncoding::FOR_BITPACK,  8, 0xFF, false, 100, false,"",          false,false,false,0});
    ts.cols.push_back({"c_mktsegment", find_col(table,"c_mktsegment"), CsfEncoding::DICT_BITPACK, 0, 0,    false, 1,   false,"",          false,false,false,0});
    ts.cols.push_back({"c_comment",    find_col(table,"c_comment"),    CsfEncoding::STRING_RAW,   0, 0xFF, false, 1,   false,"",          false,false,false,0});
    return ts;
}

static TableSpec make_part_spec(const arrow::Table* table) {
    TableSpec ts;
    ts.table_name      = "part";
    ts.sort_col_idx    = -1;
    ts.sort_ascending  = true;
    ts.sort_col2_idx   = -1;
    ts.sort2_ascending = true;

    ts.cols.push_back({"p_partkey",     find_col(table,"p_partkey"),     CsfEncoding::FOR_BITPACK,  8, 0xFF, false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"p_name",        find_col(table,"p_name"),        CsfEncoding::STRING_RAW,   0, 0xFF, false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"p_mfgr",        find_col(table,"p_mfgr"),        CsfEncoding::DICT_BITPACK, 0, 0,    false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"p_brand",       find_col(table,"p_brand"),       CsfEncoding::DICT_BITPACK, 0, 1,    false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"p_type",        find_col(table,"p_type"),        CsfEncoding::DICT_BITPACK, 0, 2,    false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"p_size",        find_col(table,"p_size"),        CsfEncoding::RAW,          1, 0xFF, false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"p_container",   find_col(table,"p_container"),   CsfEncoding::DICT_BITPACK, 0, 3,    false, 1,   false,"",false,false,false,0});
    ts.cols.push_back({"p_retailprice", find_col(table,"p_retailprice"), CsfEncoding::FOR_BITPACK,  8, 0xFF, false, 100, false,"",false,false,false,0});
    ts.cols.push_back({"p_comment",     find_col(table,"p_comment"),     CsfEncoding::STRING_RAW,   0, 0xFF, false, 1,   false,"",false,false,false,0});
    return ts;
}

static TableSpec make_supplier_spec(const arrow::Table* table) {
    TableSpec ts;
    ts.table_name      = "supplier";
    ts.sort_col_idx    = -1;
    ts.sort_ascending  = true;
    ts.sort_col2_idx   = -1;
    ts.sort2_ascending = true;

    ts.cols.push_back({"s_suppkey",   find_col(table,"s_suppkey"),   CsfEncoding::FOR_BITPACK,  8, 0xFF, false, 1,   false,"",          false,false,false,0});
    ts.cols.push_back({"s_name",      find_col(table,"s_name"),      CsfEncoding::RAW,          4, 0xFF, false, 1,   true, "Supplier#", false,false,false,0});
    ts.cols.push_back({"s_address",   find_col(table,"s_address"),   CsfEncoding::STRING_RAW,   0, 0xFF, false, 1,   false,"",          false,false,false,0});
    ts.cols.push_back({"s_nationkey", find_col(table,"s_nationkey"), CsfEncoding::RAW,          1, 0xFF, false, 1,   false,"",          false,false,false,0});
    ts.cols.push_back({"s_phone_cc",  find_col(table,"s_phone"),     CsfEncoding::RAW,          1, 0xFF, false, 1,   false,"",          true, false,false,0});
    ts.cols.push_back({"s_phone_rest",find_col(table,"s_phone"),     CsfEncoding::RAW,          8, 0xFF, false, 1,   false,"",          false,true, false,0});
    ts.cols.push_back({"s_acctbal",   find_col(table,"s_acctbal"),   CsfEncoding::FOR_BITPACK,  8, 0xFF, false, 100, false,"",          false,false,false,0});
    ts.cols.push_back({"s_comment",   find_col(table,"s_comment"),   CsfEncoding::STRING_RAW,   0, 0xFF, false, 1,   false,"",          false,false,false,0});
    return ts;
}

// ============================================================
// Special sorts
// ============================================================

static void sort_customer(std::vector<int64_t>& row_order, const arrow::Table* table) {
    int64_t n = table->num_rows();
    int mkt_col     = find_col(table, "c_mktsegment");
    int acctbal_col = find_col(table, "c_acctbal");

    std::vector<uint8_t> mkt_codes(n);
    std::vector<int64_t> acctbal(n);

    std::unordered_map<std::string,uint8_t> mkt_map;
    uint8_t next_code = 0;
    {
        ChunkCache cc; cc.init(table, mkt_col);
        for (int64_t r = 0; r < n; r++) {
            std::string s(cc.get_string(r));
            auto [it, inserted] = mkt_map.emplace(s, next_code);
            if (inserted) next_code++;
            mkt_codes[r] = it->second;
        }
    }
    {
        ChunkCache cc; cc.init(table, acctbal_col);
        for (int64_t r = 0; r < n; r++) acctbal[r] = cc.get_int64(r);
    }
    std::sort(row_order.begin(), row_order.end(), [&](int64_t a, int64_t b) {
        if (mkt_codes[a] != mkt_codes[b]) return mkt_codes[a] < mkt_codes[b];
        return acctbal[a] > acctbal[b];
    });
}

static void sort_part(std::vector<int64_t>& row_order, const arrow::Table* table) {
    int64_t n = table->num_rows();
    int brand_col = find_col(table, "p_brand");
    int cont_col  = find_col(table, "p_container");

    std::vector<uint8_t> brand_codes(n), cont_codes(n);

    auto build_codes = [&](int col, std::vector<uint8_t>& codes) {
        std::unordered_map<std::string,uint8_t> map;
        uint8_t next = 0;
        ChunkCache cache; cache.init(table, col);
        for (int64_t r = 0; r < n; r++) {
            std::string s(cache.get_string(r));
            auto [it, inserted] = map.emplace(s, next);
            if (inserted) next++;
            codes[r] = it->second;
        }
    };
    build_codes(brand_col, brand_codes);
    build_codes(cont_col,  cont_codes);

    std::sort(row_order.begin(), row_order.end(), [&](int64_t a, int64_t b) {
        if (brand_codes[a] != brand_codes[b]) return brand_codes[a] < brand_codes[b];
        return cont_codes[a] < cont_codes[b];
    });
}

// ============================================================
// BffTableInfo builder
// ============================================================
static BffTableInfo make_table_info(const CsfTableFooter& ft) {
    BffTableInfo info;
    info.name      = ft.table_name;
    info.row_count = ft.num_rows;
    for (uint32_t ci = 0; ci < ft.num_cols; ci++) {
        BffColumnInfo ci_info;
        ci_info.id            = ci;
        ci_info.name          = ft.cols[ci].name;
        ci_info.physical_type = ft.cols[ci].bff_phys;
        ci_info.nullable      = ft.cols[ci].nullable;
        info.columns.push_back(ci_info);
    }
    for (uint32_t si = 0; si < ft.num_segments; si++) {
        BffRowGroupInfo rg;
        rg.id         = si;
        rg.row_start  = uint64_t(si) * CSF_SEGMENT_ROWS;
        rg.row_count  = (si + 1 < ft.num_segments) ? CSF_SEGMENT_ROWS :
                        (ft.num_rows - uint64_t(si) * CSF_SEGMENT_ROWS);
        rg.file_offset= ft.seg_file_offsets[si];
        for (uint32_t ci = 0; ci < ft.num_cols; ci++) {
            BffColumnStats cs;
            cs.has_min = true; cs.has_max = true;
            int64_t mn = ft.zone_maps[si].col_min[ci];
            int64_t mx = ft.zone_maps[si].col_max[ci];
            cs.min_value.resize(8); memcpy(cs.min_value.data(), &mn, 8);
            cs.max_value.resize(8); memcpy(cs.max_value.data(), &mx, 8);
            cs.null_count = ft.zone_maps[si].col_null_count[ci];
            rg.column_stats.push_back(cs);
        }
        info.row_groups.push_back(rg);
    }
    return info;
}

// ============================================================
// Core table writer — unified implementation
// ============================================================
static void write_csf_table_impl(const TableSpec& spec,
                                  const arrow::Table* table,
                                  const std::string& bff_dir,
                                  const std::vector<int64_t>* ext_row_order,
                                  CsfTableFooter& ft_out) {
    int64_t total_rows = table->num_rows();
    uint32_t num_segs  = uint32_t((total_rows + CSF_SEGMENT_ROWS - 1) / CSF_SEGMENT_ROWS);
    uint32_t num_cols  = uint32_t(spec.cols.size());

    // ---- Build row order ----
    std::vector<int64_t> row_order;
    if (ext_row_order) {
        row_order = *ext_row_order;
    } else {
        row_order.resize(total_rows);
        for (int64_t i = 0; i < total_rows; i++) row_order[i] = i;

        if (spec.sort_col_idx >= 0) {
            // Precompute sort key(s) once into flat arrays for cache-friendly sort
            ChunkCache sc1; sc1.init(table, spec.sort_col_idx);
            std::vector<int64_t> keys1(total_rows);
            for (int64_t i = 0; i < total_rows; i++) keys1[i] = sc1.get_int64(i);

            if (spec.sort_col2_idx >= 0) {
                ChunkCache sc2; sc2.init(table, spec.sort_col2_idx);
                std::vector<int64_t> keys2(total_rows);
                for (int64_t i = 0; i < total_rows; i++) keys2[i] = sc2.get_int64(i);
                bool asc1 = spec.sort_ascending, asc2 = spec.sort2_ascending;
                std::sort(row_order.begin(), row_order.end(), [&](int64_t a, int64_t b) {
                    if (keys1[a] != keys1[b]) return asc1 ? keys1[a] < keys1[b] : keys1[a] > keys1[b];
                    return asc2 ? keys2[a] < keys2[b] : keys2[a] > keys2[b];
                });
            } else {
                bool asc1 = spec.sort_ascending;
                std::sort(row_order.begin(), row_order.end(), [&](int64_t a, int64_t b) {
                    return asc1 ? keys1[a] < keys1[b] : keys1[a] > keys1[b];
                });
            }
        }
    }

    // ---- Build global dicts ----
    std::vector<CsfDict> dicts;
    {
        uint8_t max_dict_id = 0;
        for (const auto& c : spec.cols)
            if (c.dict_id != 0xFF && c.dict_id >= max_dict_id) max_dict_id = c.dict_id + 1;
        dicts.resize(max_dict_id);
        for (const auto& c : spec.cols)
            if (c.dict_id != 0xFF && c.arrow_col_idx >= 0 && dicts[c.dict_id].entries.empty()) {
                dicts[c.dict_id] = build_dict(table, c.arrow_col_idx, c.dict_id, c.logical_name);
                dicts[c.dict_id].dict_id = c.dict_id;
            }
    }
    // Build reverse maps
    std::vector<std::unordered_map<std::string,uint32_t>> dict_rev(dicts.size());
    for (size_t di = 0; di < dicts.size(); di++)
        for (size_t i = 0; i < dicts[di].entries.size(); i++)
            dict_rev[di][dicts[di].entries[i]] = uint32_t(i);

    // ---- Precompute dict codes (full table, original row order) ----
    // Avoids repeated hash map lookups during segment encoding
    std::vector<std::vector<uint32_t>> precomp_dict(num_cols);
    for (uint32_t ci = 0; ci < num_cols; ci++) {
        const ColSpec& cs = spec.cols[ci];
        if (cs.encoding != CsfEncoding::DICT_BITPACK || cs.is_rle_const) continue;
        auto& rev = dict_rev[cs.dict_id];
        ChunkCache cache; cache.init(table, cs.arrow_col_idx);
        precomp_dict[ci].resize(total_rows);
        for (int64_t r = 0; r < total_rows; r++) {
            std::string_view sv = cache.get_string(r);
            std::string s(sv);
            auto it = rev.find(s);
            precomp_dict[ci][r] = (it != rev.end()) ? it->second : 0;
        }
    }

    // ---- Precompute numeric columns ----
    std::vector<std::vector<int64_t>> precomp_int(num_cols);
    for (uint32_t ci = 0; ci < num_cols; ci++) {
        const ColSpec& cs = spec.cols[ci];
        if (cs.is_rle_const) continue;
        if (cs.encoding == CsfEncoding::STRING_RAW) continue;
        if (cs.encoding == CsfEncoding::DICT_BITPACK) continue;
        if (cs.is_phone_cc || cs.is_phone_rest || cs.synthetic_pfx) continue;
        ChunkCache cache; cache.init(table, cs.arrow_col_idx);
        precomp_int[ci].resize(total_rows);
        int32_t epoch = cs.is_date ? CSF_DATE_EPOCH_OFFSET : 0;
        for (int64_t r = 0; r < total_rows; r++)
            precomp_int[ci][r] = cache.get_int64(r) - epoch;
    }

    // ---- Precompute phone cc/rest (parse once per unique phone arrow col) ----
    std::unordered_map<int, std::pair<std::vector<int64_t>,std::vector<int64_t>>> phone_precomp;
    for (uint32_t ci = 0; ci < num_cols; ci++) {
        const ColSpec& cs = spec.cols[ci];
        if (!cs.is_phone_cc && !cs.is_phone_rest) continue;
        int acol = cs.arrow_col_idx;
        if (phone_precomp.count(acol)) continue;
        ChunkCache cache; cache.init(table, acol);
        auto& [ccs, rests] = phone_precomp[acol];
        ccs.resize(total_rows); rests.resize(total_rows);
        for (int64_t r = 0; r < total_rows; r++) {
            auto [c, rest] = parse_phone(cache.get_string(r));
            ccs[r]   = int64_t(c);
            rests[r] = int64_t(rest);
        }
    }

    // ---- Precompute synthetic prefix (name suffix) ----
    std::vector<std::vector<int64_t>> precomp_pfx(num_cols);
    for (uint32_t ci = 0; ci < num_cols; ci++) {
        const ColSpec& cs = spec.cols[ci];
        if (!cs.synthetic_pfx) continue;
        ChunkCache cache; cache.init(table, cs.arrow_col_idx);
        precomp_pfx[ci].resize(total_rows);
        size_t pfx_len = cs.prefix_str.size();
        for (int64_t r = 0; r < total_rows; r++)
            precomp_pfx[ci][r] = int64_t(parse_name_suffix(cache.get_string(r), pfx_len));
    }

    // ---- Build CsfColMeta ----
    std::vector<CsfColMeta> col_meta;
    col_meta.reserve(num_cols);
    for (uint32_t ci = 0; ci < num_cols; ci++) {
        const ColSpec& c = spec.cols[ci];
        CsfColMeta m;
        m.name      = c.logical_name;
        m.encoding  = c.encoding;
        m.phys_bytes= c.phys_bytes;
        m.dict_id   = c.dict_id;
        m.is_signed = false;
        m.scale     = (c.decimal_scale > 0) ? c.decimal_scale : 1;
        m.date_epoch= c.is_date ? CSF_DATE_EPOCH_OFFSET : 0;
        m.nullable  = true;
        m.synthetic_prefix = c.synthetic_pfx;
        m.prefix_str = c.prefix_str;
        m.split_phone = c.is_phone_cc || c.is_phone_rest;
        if (c.encoding == CsfEncoding::STRING_RAW) m.bff_phys = BffPhysicalType::String;
        else if (c.synthetic_pfx)                  m.bff_phys = BffPhysicalType::UInt32;
        else if (c.is_phone_cc)                    m.bff_phys = BffPhysicalType::UInt8;
        else if (c.is_date)                        m.bff_phys = BffPhysicalType::Int32;
        else if (c.phys_bytes <= 1)                m.bff_phys = BffPhysicalType::UInt8;
        else if (c.phys_bytes <= 4)                m.bff_phys = BffPhysicalType::Int32;
        else                                       m.bff_phys = BffPhysicalType::Int64;
        if (c.dict_id != 0xFF) {
            size_t ne = (c.dict_id < dicts.size()) ? dicts[c.dict_id].entries.size() : 2;
            m.bit_width = bits_needed(ne > 0 ? uint64_t(ne-1) : 1);
        }
        col_meta.push_back(m);
    }

    // ---- Cache string chunks for STRING_RAW columns ----
    std::unordered_map<int, ChunkCache> str_caches;
    for (uint32_t ci = 0; ci < num_cols; ci++) {
        const ColSpec& cs = spec.cols[ci];
        if (cs.encoding == CsfEncoding::STRING_RAW && !cs.synthetic_pfx && cs.arrow_col_idx >= 0)
            if (!str_caches.count(cs.arrow_col_idx))
                str_caches[cs.arrow_col_idx].init(table, cs.arrow_col_idx);
    }

    // ---- Open file with buffered writer ----
    FileWriter fw;
    fw.open(bff_dir + "/" + spec.table_name + ".csf");

    CsfFileHeader fhdr;
    memset(&fhdr, 0, sizeof(fhdr));
    memcpy(fhdr.magic, "CSFv0001", 8);
    fhdr.version       = CSF_VERSION;
    fhdr.num_columns   = num_cols;
    fhdr.num_segments  = num_segs;
    fhdr.segment_size  = CSF_SEGMENT_ROWS;
    fhdr.clustering_col= (spec.sort_col_idx >= 0) ? int16_t(spec.sort_col_idx) : -1;
    fhdr.clustering_dir= spec.sort_ascending ? 0 : 1;
    fhdr.flags         = 0;
    fhdr.footer_offset = 0;
    fhdr.footer_length = 0;
    fw.write_bytes(&fhdr, sizeof(fhdr));

    // ---- Init footer ----
    ft_out.table_name   = spec.table_name;
    ft_out.num_rows     = uint64_t(total_rows);
    ft_out.num_segments = num_segs;
    ft_out.num_cols     = num_cols;
    ft_out.cols         = col_meta;
    ft_out.dicts        = dicts;
    ft_out.seg_file_offsets.resize(num_segs);
    ft_out.col_block_offsets.resize(uint64_t(num_segs) * num_cols);
    ft_out.zone_maps.resize(num_segs);

    // Reusable block buffer
    std::vector<uint8_t> block_buf;
    block_buf.reserve(CSF_SEGMENT_ROWS * 9);

    // ---- Write segments ----
    for (uint32_t seg = 0; seg < num_segs; seg++) {
        int64_t seg_start = int64_t(seg) * CSF_SEGMENT_ROWS;
        int64_t seg_end   = std::min(seg_start + (int64_t)CSF_SEGMENT_ROWS, total_rows);
        uint32_t seg_rows = uint32_t(seg_end - seg_start);

        uint64_t seg_base = uint64_t(fw.tell());
        ft_out.seg_file_offsets[seg] = seg_base;

        CsfSegZoneMap& zm = ft_out.zone_maps[seg];
        zm.col_min.assign(num_cols, std::numeric_limits<int64_t>::max());
        zm.col_max.assign(num_cols, std::numeric_limits<int64_t>::lowest());
        zm.col_bitset.assign(num_cols, 0ULL);
        zm.col_null_count.assign(num_cols, 0);

        for (uint32_t ci = 0; ci < num_cols; ci++) {
            const ColSpec& cs = spec.cols[ci];
            ft_out.col_block_offsets[seg * num_cols + ci] = uint64_t(fw.tell()) - seg_base;

            block_buf.clear();
            CsfBlockHeader hdr_dummy{};

            if (cs.is_rle_const) {
                write_rle_block(cs.rle_value, seg_rows, block_buf, hdr_dummy);
                zm.col_min[ci] = cs.rle_value;
                zm.col_max[ci] = cs.rle_value;

            } else if (cs.encoding == CsfEncoding::STRING_RAW && !cs.synthetic_pfx) {
                std::vector<std::string_view> strs(seg_rows);
                auto& sc = str_caches[cs.arrow_col_idx];
                for (uint32_t r = 0; r < seg_rows; r++) {
                    int64_t row = row_order[seg_start + r];
                    if (!sc.is_null(row)) strs[r] = sc.get_string(row);
                }
                write_string_block(strs, block_buf, hdr_dummy);

            } else if (cs.synthetic_pfx) {
                const auto& pdata = precomp_pfx[ci];
                std::vector<int64_t> vals(seg_rows);
                int64_t vmin = std::numeric_limits<int64_t>::max();
                int64_t vmax = std::numeric_limits<int64_t>::lowest();
                for (uint32_t r = 0; r < seg_rows; r++) {
                    int64_t v = pdata[row_order[seg_start + r]];
                    vals[r] = v;
                    if (v < vmin) vmin = v;
                    if (v > vmax) vmax = v;
                }
                write_raw_block(vals, 4, block_buf, hdr_dummy);
                zm.col_min[ci] = vmin; zm.col_max[ci] = vmax;

            } else if (cs.is_phone_cc) {
                const auto& ccs = phone_precomp.at(cs.arrow_col_idx).first;
                std::vector<int64_t> vals(seg_rows);
                int64_t vmin = std::numeric_limits<int64_t>::max();
                int64_t vmax = std::numeric_limits<int64_t>::lowest();
                uint64_t bitset = 0;
                for (uint32_t r = 0; r < seg_rows; r++) {
                    int64_t v = ccs[row_order[seg_start + r]];
                    vals[r] = v;
                    if (v < vmin) vmin = v;
                    if (v > vmax) vmax = v;
                    if (v >= 10 && v <= 34) bitset |= (1ULL << (v-10));
                }
                write_raw_block(vals, 1, block_buf, hdr_dummy);
                zm.col_min[ci] = vmin; zm.col_max[ci] = vmax; zm.col_bitset[ci] = bitset;

            } else if (cs.is_phone_rest) {
                const auto& rests = phone_precomp.at(cs.arrow_col_idx).second;
                std::vector<int64_t> vals(seg_rows);
                for (uint32_t r = 0; r < seg_rows; r++)
                    vals[r] = rests[row_order[seg_start + r]];
                write_raw_block(vals, 8, block_buf, hdr_dummy);

            } else if (cs.encoding == CsfEncoding::DICT_BITPACK) {
                const auto& codes_all = precomp_dict[ci];
                const auto& d = dicts[cs.dict_id];
                uint8_t bits = bits_needed(uint64_t(d.entries.empty() ? 1 : d.entries.size()-1));
                if (bits == 0) bits = 1;
                std::vector<uint64_t> codes(seg_rows);
                uint64_t bitset = 0;
                for (uint32_t r = 0; r < seg_rows; r++) {
                    uint64_t code = codes_all[row_order[seg_start + r]];
                    codes[r] = code;
                    bitset |= (1ULL << code);
                }
                write_dict_bitpack_block(codes, bits, uint16_t(cs.dict_id), block_buf, hdr_dummy);
                zm.col_min[ci] = 0;
                zm.col_max[ci] = int64_t(d.entries.empty() ? 0 : d.entries.size()-1);
                zm.col_bitset[ci] = bitset;

            } else if (cs.encoding == CsfEncoding::FOR_BITPACK) {
                const auto& data = precomp_int[ci];
                std::vector<int64_t> vals(seg_rows);
                int64_t vmin = std::numeric_limits<int64_t>::max();
                int64_t vmax = std::numeric_limits<int64_t>::lowest();
                for (uint32_t r = 0; r < seg_rows; r++) {
                    int64_t v = data[row_order[seg_start + r]];
                    vals[r] = v;
                    if (v < vmin) vmin = v;
                    if (v > vmax) vmax = v;
                }
                int64_t base_used = 0;
                write_for_bitpack_block(vals, block_buf, hdr_dummy, base_used);
                zm.col_min[ci] = vmin; zm.col_max[ci] = vmax;

            } else if (cs.encoding == CsfEncoding::DELTA_BITPACK) {
                const auto& data = precomp_int[ci];
                std::vector<int64_t> vals(seg_rows);
                int64_t vmin = std::numeric_limits<int64_t>::max();
                int64_t vmax = std::numeric_limits<int64_t>::lowest();
                for (uint32_t r = 0; r < seg_rows; r++) {
                    int64_t v = data[row_order[seg_start + r]];
                    vals[r] = v;
                    if (v < vmin) vmin = v;
                    if (v > vmax) vmax = v;
                }
                int64_t first_used = 0;
                write_delta_bitpack_block(vals, block_buf, hdr_dummy, first_used);
                zm.col_min[ci] = vmin; zm.col_max[ci] = vmax;

            } else {
                // RAW numeric
                const auto& data = precomp_int[ci];
                std::vector<int64_t> vals(seg_rows);
                int64_t vmin = std::numeric_limits<int64_t>::max();
                int64_t vmax = std::numeric_limits<int64_t>::lowest();
                for (uint32_t r = 0; r < seg_rows; r++) {
                    int64_t v = data[row_order[seg_start + r]];
                    vals[r] = v;
                    if (v < vmin) vmin = v;
                    if (v > vmax) vmax = v;
                }
                write_raw_block(vals, cs.phys_bytes, block_buf, hdr_dummy);
                zm.col_min[ci] = vmin; zm.col_max[ci] = vmax;
            }

            fw.write_bytes(block_buf.data(), block_buf.size());
        }
    }

    // ---- Footer ----
    auto footer_bytes = serialize_table_footer(ft_out);
    uint64_t footer_off = uint64_t(fw.tell());
    fw.write_bytes(footer_bytes.data(), footer_bytes.size());
    fhdr.footer_offset = footer_off;
    fhdr.footer_length = uint32_t(footer_bytes.size());
    fw.pwrite_at(&fhdr, sizeof(fhdr), 0);
    fw.close();
}

// ============================================================
// Top-level write functions
// ============================================================

BffDataset* write_bff_from_parquet(
    std::string /*parquet_dir*/,
    std::string bff_dir,
    const BffWriteOptions& /*options*/) {
    std::error_code ec;
    std::filesystem::create_directories(bff_dir, ec);
    auto* ds = new BffDataset();
    ds->root_path = std::move(bff_dir);
    ds->has_footer = false;
    return ds;
}

BffDataset* write_bff_from_parquet_tables(
    const ParquetTables* tables,
    std::string bff_dir,
    const BffWriteOptions& /*options*/) {
    std::error_code ec;
    std::filesystem::create_directories(bff_dir, ec);

    auto* dataset = new BffDataset();
    dataset->root_path = bff_dir;
    dataset->footer.info.root_path = bff_dir;

    auto write_table = [&](const std::string& name,
                            const arrow::Table* table,
                            const TableSpec& spec,
                            const std::vector<int64_t>* custom_order) {
        CsfTableFooter ft;
        write_csf_table_impl(spec, table, bff_dir, custom_order, ft);
        dataset->footer.csf_tables[name] = ft;
        dataset->footer.info.tables.push_back(make_table_info(ft));
    };

    {
        auto t = tables->lineitem.get();
        write_table("lineitem", t, make_lineitem_spec(t), nullptr);
    }
    {
        auto t = tables->orders.get();
        write_table("orders", t, make_orders_spec(t), nullptr);
    }
    {
        auto t = tables->customer.get();
        auto spec = make_customer_spec(t);
        std::vector<int64_t> ord(t->num_rows());
        for (int64_t i = 0; i < t->num_rows(); i++) ord[i] = i;
        sort_customer(ord, t);
        spec.sort_col_idx = -1;
        write_table("customer", t, spec, &ord);
    }
    {
        auto t = tables->part.get();
        auto spec = make_part_spec(t);
        std::vector<int64_t> ord(t->num_rows());
        for (int64_t i = 0; i < t->num_rows(); i++) ord[i] = i;
        sort_part(ord, t);
        spec.sort_col_idx = -1;
        write_table("part", t, spec, &ord);
    }
    {
        auto t = tables->supplier.get();
        write_table("supplier", t, make_supplier_spec(t), nullptr);
    }

    dataset->has_footer = true;
    return dataset;
}
