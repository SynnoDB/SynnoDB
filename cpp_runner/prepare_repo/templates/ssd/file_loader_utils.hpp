#pragma once

#include "buffer_pool.hpp"
#include "column_handle.hpp"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <type_traits>
#include <vector>

#include <arrow/array.h>
#include <arrow/chunked_array.h>
#include <arrow/table.h>
#include <parquet/arrow/reader.h>

struct ParquetFileScanner {
    std::unique_ptr<parquet::arrow::FileReader> reader;
    int num_row_groups = 0;

    explicit ParquetFileScanner(const std::string& path);
    ~ParquetFileScanner();

    std::shared_ptr<arrow::Table> read_row_group(
        int row_group,
        const std::vector<int>& column_indices = {});
};

std::string_view extract_sv(const arrow::Array* arr, int64_t i);
int64_t extract_int64(const arrow::Array* arr, int64_t i);
int32_t extract_int32(const arrow::Array* arr, int64_t i);

struct FdStream {
    int fd_ = -1;
    std::unique_ptr<char, void (*)(void*)> buf_{nullptr, nullptr};
    std::size_t cap_ = 0;
    std::size_t pos_ = 0;

    FdStream();
    ~FdStream();

    FdStream(const FdStream&) = delete;
    FdStream& operator=(const FdStream&) = delete;

    void open(const std::string& path);
    void flush_raw();
    void write(const void* data, std::size_t n);
    void close();
};

void write_int32_chunk_fast(FdStream& out, const arrow::Array* chunk);
void write_int64_chunk_fast(FdStream& out, const arrow::Array* chunk);

struct StringWriter {
    FdStream off_f_;
    FdStream byte_f_;
    uint64_t running_ = 0;
    int64_t nrows_ = 0;

    static constexpr std::size_t OFF_BATCH = 65536;
    uint64_t off_buf_[OFF_BATCH];
    std::size_t off_n_ = 0;

    void open(const std::string& offsets_path, const std::string& bytes_path);
    void flush_offsets();
    void push_offset();
    void write_chunk(const arrow::Array* arr);
    void finish();

private:
    template <typename ArrowOffset>
    void write_offsets_from_arrow_offsets(const ArrowOffset* arrow_offsets, int64_t len);
};

// Register a flat (no-padding) fixed-width column file: file size is exactly
// num_rows * sizeof(T) bytes.
//
// Safe only when sizeof(T) divides BP_PAGE_BYTES exactly — otherwise the last
// row on each page would straddle the page boundary and ColumnHandle::pin_range
// would throw "computed range exceeds pinned page" at runtime once a column
// has more than ROWS_PER_PAGE rows. The static_assert catches this at compile
// time. For non-dividing element sizes (e.g. std::array<char,15> or
// std::array<char,25>) use reg_page_aligned_fixed_width<T> with a file written
// by write_fixed_char_col_aligned / finish_fixed_char_col_aligned.
template <typename T>
inline ColumnHandle<T> reg_fixed_width(BufferPool* pool,
                                       const std::string& path,
                                       int64_t num_rows) {
    static_assert(BP_PAGE_BYTES % static_cast<int64_t>(sizeof(T)) == 0,
                  "reg_fixed_width<T> is only safe when sizeof(T) divides BP_PAGE_BYTES. "
                  "Use StringColumnHandle or reg_page_aligned_fixed_width<T> for fixed-char data.");
    int64_t nbytes = num_rows * static_cast<int64_t>(sizeof(T));
    int cid = pool->register_column(path, nbytes);
    return ColumnHandle<T>{pool, cid, num_rows};
}

// Register a fixed-width column file written with page-aligned padding.
//
// Use when sizeof(T) does NOT divide BP_PAGE_BYTES. The on-disk layout must be
// ceil(num_rows / rows_per_page) full BP_PAGE_BYTES pages, where each page
// holds exactly rows_per_page rows of data followed by
// (BP_PAGE_BYTES - rows_per_page * sizeof(T)) zero-pad bytes. The last (partial)
// page is also zero-padded out to BP_PAGE_BYTES.
//
// This layout makes ColumnHandle<T>::page_for_row(row) and ::page_row_begin(p)
// agree with the on-disk byte ranges that BufferPool::pin_page reads, so
// pin_range never straddles a page boundary. ColumnHandle::num_rows still
// stores the logical row count, so accessors trim the trailing pad rows.
//
// Pair with write_fixed_char_col_aligned + finish_fixed_char_col_aligned to
// produce the file in this layout.
template <typename T>
inline ColumnHandle<T> reg_page_aligned_fixed_width(BufferPool* pool,
                                                    const std::string& path,
                                                    int64_t num_rows) {
    static_assert(std::is_trivially_copyable_v<T>,
                  "reg_page_aligned_fixed_width<T> requires flat binary values");
    constexpr int64_t rows_per_page =
        BP_PAGE_BYTES / static_cast<int64_t>(sizeof(T));
    static_assert(rows_per_page > 0, "T is larger than one buffer-pool page");

    int64_t pages = (num_rows + rows_per_page - 1) / rows_per_page;
    int64_t nbytes = pages * BP_PAGE_BYTES;
    int cid = pool->register_column(path, nbytes);
    return ColumnHandle<T>{pool, cid, num_rows};
}

ColumnHandle<int32_t> reg_int32(BufferPool* pool,
                                const std::string& path,
                                int64_t num_rows);
ColumnHandle<int64_t> reg_int64(BufferPool* pool,
                                const std::string& path,
                                int64_t num_rows);
StringColumnHandle reg_string(BufferPool* pool,
                              const std::string& offsets_path,
                              const std::string& bytes_path,
                              int64_t num_rows);

bool all_exist(const std::vector<std::string>& paths);
void write_col_int32(FdStream& f, const arrow::ChunkedArray* ca);
void write_col_int64(FdStream& f, const arrow::ChunkedArray* ca);
void write_col_string(StringWriter& sw, const arrow::ChunkedArray* ca);
// Append a fixed-char column chunk to a page-aligned file (see
// reg_page_aligned_fixed_width for the layout). Each row is written as `width`
// bytes — shorter strings are right-zero-padded, longer strings are truncated.
// `rows_written` is the running total of rows already appended to this file
// across previous chunks; it is read and updated. After all chunks are
// processed, call finish_fixed_char_col_aligned exactly once to pad the last
// (partial) page.
void write_fixed_char_col_aligned(FdStream& f,
                                  const arrow::ChunkedArray* ca,
                                  int width,
                                  int64_t& rows_written);

// Pad the file written by write_fixed_char_col_aligned so its size equals
// ceil(rows_written / rows_per_page) * BP_PAGE_BYTES. Must be called exactly
// once per file, after the last chunk. No-op when rows_written lands exactly
// on a page boundary (loop already padded) or when no rows were written.
void finish_fixed_char_col_aligned(FdStream& f, int64_t rows_written, int width);

template <typename ArrowOffset>
void StringWriter::write_offsets_from_arrow_offsets(const ArrowOffset* arrow_offsets,
                                                    int64_t len) {
    for (int64_t i = 0; i < len;) {
        int64_t n = std::min<int64_t>(static_cast<int64_t>(OFF_BATCH), len - i);
        for (int64_t j = 0; j < n; ++j) {
            off_buf_[j] =
                static_cast<uint64_t>(arrow_offsets[i + j + 1] - arrow_offsets[i + j]);
        }

        uint64_t acc = running_;
        for (int64_t j = 0; j < n; ++j) {
            uint64_t len_bytes = off_buf_[j];
            off_buf_[j] = acc;
            acc += len_bytes;
        }

        off_f_.write(off_buf_, static_cast<std::size_t>(n) * sizeof(uint64_t));
        running_ = acc;
        i += n;
    }
}
