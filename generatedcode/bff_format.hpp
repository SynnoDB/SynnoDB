#pragma once
// FILE_VERSION: 2
// Concrete on-disk and in-memory definitions for the CSF (Columnar Segment
// File) bespoke file format.

#include "ingest_types.hpp"

#include <cstdint>
#include <cstring>
#include <string>
#include <unordered_map>
#include <vector>

// ---------------------------------------------------------------------------
// Format constants
// ---------------------------------------------------------------------------
static constexpr uint64_t CSF_MAGIC_U64       = 0x3130303076465343ULL; // "CSFv0001"
static constexpr uint32_t CSF_VERSION         = 2;
static constexpr uint32_t CSF_SEGMENT_ROWS    = 131072;
static constexpr uint16_t CSF_BLOCK_MAGIC     = 0xC5F0;
static constexpr int32_t  CSF_DATE_EPOCH_OFFSET = 8035; // days 1970-01-01 -> 1992-01-01

// ---------------------------------------------------------------------------
// On-disk file header (64 bytes, at offset 0)
// ---------------------------------------------------------------------------
struct CsfFileHeader {
    uint8_t  magic[8];
    uint32_t version;
    uint32_t num_columns;
    uint64_t num_segments;
    uint32_t segment_size;
    int16_t  clustering_col;  // -1 = unsorted
    uint8_t  clustering_dir;  // 0=asc 1=desc
    uint8_t  flags;
    uint64_t footer_offset;
    uint32_t footer_length;
    uint8_t  reserved[20];
} __attribute__((packed));
static_assert(sizeof(CsfFileHeader) == 64, "CsfFileHeader must be 64 bytes");

// ---------------------------------------------------------------------------
// Column encoding types
// ---------------------------------------------------------------------------
enum class CsfEncoding : uint8_t {
    RAW           = 0,
    FOR_BITPACK   = 1,
    DELTA_BITPACK = 2,
    DICT_BITPACK  = 3,
    RLE           = 4,
    STRING_RAW    = 5,
};

// ---------------------------------------------------------------------------
// On-disk column block header (16 bytes)
// ---------------------------------------------------------------------------
struct CsfBlockHeader {
    uint16_t magic;
    uint8_t  encoding;
    uint8_t  bit_width;
    uint32_t compressed_bytes;
    uint32_t raw_bytes;
    uint32_t num_values;
} __attribute__((packed));
static_assert(sizeof(CsfBlockHeader) == 16, "CsfBlockHeader must be 16 bytes");

// Encoding sub-headers immediately after CsfBlockHeader:
struct CsfForHeader   { int64_t  base;  } __attribute__((packed)); // 8 bytes
struct CsfDeltaHeader { int64_t  first; } __attribute__((packed)); // 8 bytes
struct CsfDictHeader  { uint16_t dict_id; } __attribute__((packed)); // 2 bytes
struct CsfStringHeader {
    uint32_t offset_compressed_bytes;
    uint32_t payload_compressed_bytes;
} __attribute__((packed)); // 8 bytes

// ---------------------------------------------------------------------------
// In-memory column metadata
// ---------------------------------------------------------------------------
struct CsfColMeta {
    std::string name;
    CsfEncoding encoding    = CsfEncoding::RAW;
    uint8_t     phys_bytes  = 8;   // bytes per decoded element
    uint8_t     bit_width   = 0;   // for bitpack encodings
    uint8_t     dict_id     = 0xFF;// 0xFF = no dict
    bool        is_signed   = false;
    int32_t     scale       = 1;   // logical_value = stored_int / scale
    int32_t     date_epoch  = 0;   // non-zero for date columns (add to get unix days)
    bool        nullable    = false;
    bool        synthetic_prefix = false;
    std::string prefix_str;
    bool        split_phone = false; // true for *_cc / *_rest sub-cols
    BffPhysicalType bff_phys = BffPhysicalType::Int64;
};

// ---------------------------------------------------------------------------
// In-memory global dictionary
// ---------------------------------------------------------------------------
struct CsfDict {
    uint8_t     dict_id   = 0;
    std::string col_name;
    std::vector<std::string> entries; // entries[code] = string value
};

// ---------------------------------------------------------------------------
// Per-segment zone map (generic: one min/max/bitset/nullcount per column)
// ---------------------------------------------------------------------------
struct CsfSegZoneMap {
    std::vector<int64_t>  col_min;
    std::vector<int64_t>  col_max;
    std::vector<uint64_t> col_bitset;   // for DICT_BITPACK cols (up to 64 entries)
    std::vector<uint64_t> col_null_count;
    std::vector<uint8_t>  bloom;        // optional Bloom filter bytes
};

// ---------------------------------------------------------------------------
// Full per-table CSF footer (decoded into RAM on first open)
// ---------------------------------------------------------------------------
struct CsfTableFooter {
    std::string table_name;
    uint64_t    num_rows     = 0;
    uint32_t    num_segments = 0;
    uint32_t    num_cols     = 0;
    std::vector<CsfColMeta>     cols;
    std::vector<CsfDict>        dicts;
    std::vector<uint64_t>       seg_file_offsets;   // [num_segments]
    // col_block_offsets[seg*num_cols + col] = byte offset rel to seg start
    std::vector<uint64_t>       col_block_offsets;
    std::vector<CsfSegZoneMap>  zone_maps;          // [num_segments]
};

// ---------------------------------------------------------------------------
// Extended BFF handles
// ---------------------------------------------------------------------------
struct BffFooter {
    BffFooterInfo info;
    std::unordered_map<std::string, CsfTableFooter> csf_tables;
};

struct BffDataset {
    std::string    root_path;
    BffOpenOptions options;
    bool           has_footer = false;
    BffFooter      footer;
};

struct BffTable {
    BffDataset*     dataset    = nullptr;
    BffTableInfo    info;
    std::string     table_name;
    std::string     file_path;
    CsfTableFooter* csf_footer = nullptr; // points into dataset->footer
    int             fd         = -1;
};

struct Database {
    BffDataset*      dataset = nullptr;
    const BffFooter* footer  = nullptr;
};

// ---------------------------------------------------------------------------
// Simple byte-buffer helpers for footer serialization/deserialization
// ---------------------------------------------------------------------------
struct ByteWriter {
    std::vector<uint8_t>& buf;
    explicit ByteWriter(std::vector<uint8_t>& b) : buf(b) {}
    void write_u8 (uint8_t  v) { buf.push_back(v); }
    void write_u16(uint16_t v) { buf.push_back(uint8_t(v)); buf.push_back(uint8_t(v>>8)); }
    void write_u32(uint32_t v) { for(int i=0;i<4;i++) buf.push_back(uint8_t(v>>(8*i))); }
    void write_u64(uint64_t v) { for(int i=0;i<8;i++) buf.push_back(uint8_t(v>>(8*i))); }
    void write_i64(int64_t  v) { write_u64(static_cast<uint64_t>(v)); }
    void write_str(const std::string& s) {
        write_u32(uint32_t(s.size()));
        for(char c : s) buf.push_back(uint8_t(c));
    }
    void write_bytes(const uint8_t* p, size_t n) { buf.insert(buf.end(), p, p+n); }
    size_t pos() const { return buf.size(); }
};

struct ByteReader {
    const uint8_t* p;
    size_t         remaining;
    ByteReader(const uint8_t* data, size_t size) : p(data), remaining(size) {}
    uint8_t  read_u8()  {
        if(remaining<1) return 0;
        uint8_t v=*p++; remaining--; return v;
    }
    uint16_t read_u16() {
        if(remaining<2) return 0;
        uint16_t v=uint16_t(p[0])|(uint16_t(p[1])<<8); p+=2; remaining-=2; return v;
    }
    uint32_t read_u32() {
        if(remaining<4) return 0;
        uint32_t v=uint32_t(p[0])|(uint32_t(p[1])<<8)|(uint32_t(p[2])<<16)|(uint32_t(p[3])<<24);
        p+=4; remaining-=4; return v;
    }
    uint64_t read_u64() {
        uint64_t lo=read_u32(), hi=read_u32(); return lo|(hi<<32);
    }
    int64_t read_i64() { return int64_t(read_u64()); }
    std::string read_str() {
        uint32_t len=read_u32();
        if(remaining<len) return {};
        std::string s(reinterpret_cast<const char*>(p), len);
        p+=len; remaining-=len; return s;
    }
    void read_bytes(uint8_t* dst, size_t n) {
        if(remaining<n) { memset(dst,0,n); return; }
        memcpy(dst, p, n); p+=n; remaining-=n;
    }
    bool ok() const { return true; }
};

// ---------------------------------------------------------------------------
// Footer section identifiers (used in the TOC)
// ---------------------------------------------------------------------------
enum CsfFooterSection : uint8_t {
    FOOTER_SECT_SCHEMA      = 0,
    FOOTER_SECT_DICTS       = 1,
    FOOTER_SECT_SEG_OFFSETS = 2,
    FOOTER_SECT_ZONE_MAPS   = 3,
    FOOTER_SECT_BLOOM       = 4,
    FOOTER_SECT_COUNT       = 5
};

// Footer TOC entry (12 bytes)
struct CsfFooterTocEntry {
    uint8_t  section_id;
    uint8_t  pad[3];
    uint32_t offset;   // relative to start of footer bytes
    uint32_t length;
} __attribute__((packed));
static_assert(sizeof(CsfFooterTocEntry) == 12, "CsfFooterTocEntry must be 12 bytes");

// ---------------------------------------------------------------------------
// Inline dict helper
// ---------------------------------------------------------------------------
inline const std::string& csf_dict_lookup(const CsfDict& dict, uint32_t code) {
    static const std::string empty_str;
    if(code < dict.entries.size()) return dict.entries[code];
    return empty_str;
}
