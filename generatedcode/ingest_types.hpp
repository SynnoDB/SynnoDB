#pragma once

#include <cstdint>
#include <string>
#include <vector>

struct BffDataset;
struct BffTable;
struct BffFooter;

enum class BffIoMode : std::uint8_t {
    Buffered = 0,
    Direct = 1,
    MemoryMapped = 2,
};

enum class BffPhysicalType : std::uint8_t {
    Int8 = 0,
    Int16 = 1,
    Int32 = 2,
    Int64 = 3,
    UInt8 = 4,
    UInt16 = 5,
    UInt32 = 6,
    UInt64 = 7,
    Float32 = 8,
    Float64 = 9,
    Decimal128 = 10,
    Date32 = 11,
    Binary = 12,
    String = 13,
    Boolean = 14,
};

enum class BffEncoding : std::uint8_t {
    Plain = 0,
    Dictionary = 1,
    Rle = 2,
    Delta = 3,
    FrameOfReference = 4,
    BitPacked = 5,
    PatchedBase = 6,
};

enum class BffBufferStorage : std::uint8_t {
    Owned = 0,
    Borrowed = 1,
    MemoryMapped = 2,
    DirectIo = 3,
    External = 4,
};

struct BffColumnStats {
    bool has_min = false;
    bool has_max = false;
    std::vector<std::uint8_t> min_value;
    std::vector<std::uint8_t> max_value;
    std::uint64_t null_count = 0;
    std::uint64_t distinct_count = 0;
};

struct BffColumnInfo {
    std::uint32_t id = 0;
    std::string name;
    BffPhysicalType physical_type = BffPhysicalType::Int64;
    bool nullable = false;
};

struct BffPageInfo {
    std::uint32_t row_group_id = 0;
    std::uint32_t column_id = 0;
    std::uint32_t page_id = 0;
    std::uint64_t file_offset = 0;
    std::uint64_t compressed_size = 0;
    std::uint64_t uncompressed_size = 0;
    std::uint64_t row_count = 0;
    BffEncoding encoding = BffEncoding::Plain;
    BffColumnStats stats;
};

struct BffRowGroupInfo {
    std::uint32_t id = 0;
    std::uint64_t row_start = 0;
    std::uint64_t row_count = 0;
    std::uint64_t file_offset = 0;
    std::uint64_t byte_size = 0;
    std::vector<BffColumnStats> column_stats;
};

struct BffTableInfo {
    std::string name;
    std::string path;
    std::uint64_t row_count = 0;
    std::uint64_t byte_size = 0;
    std::vector<BffColumnInfo> columns;
    std::vector<BffRowGroupInfo> row_groups;
};

struct BffFooterInfo {
    std::string root_path;
    std::uint32_t format_version = 1;
    std::uint64_t footer_offset = 0;
    std::uint64_t footer_size = 0;
    std::uint64_t footer_checksum = 0;
    std::vector<BffTableInfo> tables;
};

struct BffOpenOptions {
    BffIoMode io_mode = BffIoMode::Buffered;
    bool cache_footer = true;
    bool validate_footer_checksum = true;
    std::uint64_t direct_io_alignment = 4096;
};

struct BffWriteOptions {
    bool overwrite = false;
    std::uint64_t target_row_group_rows = 0;
    std::uint64_t target_page_bytes = 0;
    bool write_page_stats = true;
    bool write_row_group_stats = true;
    bool write_footer_checksum = true;
};

struct BffReadOptions {
    BffIoMode io_mode = BffIoMode::Buffered;
    bool verify_checksum = true;
    bool decompress = false;
    std::uint64_t direct_io_alignment = 4096;
};

struct BffColumnSelection {
    // Empty means all columns.
    std::vector<std::uint32_t> column_ids;
};

struct BffBuffer {
    const std::uint8_t* data = nullptr;
    std::uint64_t size = 0;
    std::uint64_t alignment = 1;
    std::uint64_t file_offset = 0;
    BffBufferStorage storage = BffBufferStorage::Owned;
    bool immutable = true;
    bool encoded = true;
    void* owner = nullptr;
};
