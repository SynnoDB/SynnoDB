#pragma once

#include <algorithm>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <type_traits>

#include "buffer_pool.hpp"

// Typed accessor for a single column stored as a flat binary file of T[].
// Pages are loaded on demand from the shared BufferPool.
//
// Typical iteration pattern: scan one column page at a time.
//
//   for (int64_t pg = 0; pg < col.num_pages(); ++pg) {
//       int64_t count;
//       const T* ptr = col.pin_page(pg, &count);
//       for (int64_t i = 0; i < count; ++i) {
//           // ... process ptr[i] ...
//       }
//       col.unpin_page(pg);
//   }
//
// For multi-column scans, different C++ element sizes mean different columns
// have different rows-per-page. Do not assume page N in two columns covers the
// same row range. Instead, pick a logical row range and pin that range from
// every column with pin_range().
//
//   for (int64_t row = 0; row < db->col_a.num_rows; ) {
//       int64_t chunk = db->col_a.contiguous_rows(row, db->col_a.num_rows - row);
//       chunk = std::min(chunk, db->col_b.contiguous_rows(row, chunk));
//       auto a = db->col_a.pin_range(row, chunk);
//       auto b = db->col_b.pin_range(row, chunk);
//       for (int64_t i = 0; i < chunk; ++i) {
//           // a.data[i] and b.data[i] are values for logical row row + i
//       }
//       db->col_a.unpin_page(a.page_idx);
//       db->col_b.unpin_page(b.page_idx);
//       row += chunk;
//   }
//
// Pin only the pages you actively need; unpin before moving on.
// Never hold pins across unrelated operations — pin pressure evicts other pages.

template<typename T>
struct ColumnHandle {
    static_assert(std::is_trivially_copyable_v<T>, "ColumnHandle<T> requires flat binary trivially-copyable values");
    static_assert(sizeof(T) <= BP_PAGE_BYTES, "ColumnHandle<T> element size exceeds buffer-pool page size");

    BufferPool* pool     = nullptr;
    int         col_id   = -1;
    int64_t     num_rows = 0;

    // Integer division: when sizeof(T) does not divide BP_PAGE_BYTES (e.g.
    // std::array<char,25>), each on-disk page must be padded to BP_PAGE_BYTES
    // or pin_range() will throw. Construct such handles via
    // reg_page_aligned_fixed_width<T>() with files written by
    // write_fixed_char_col_aligned() / finish_fixed_char_col_aligned().
    static constexpr int64_t ROWS_PER_PAGE = BP_PAGE_BYTES / static_cast<int64_t>(sizeof(T));

    struct PinnedRange {
        const T* data       = nullptr;
        int64_t  page_idx   = -1;
        int64_t  row_begin  = 0;
        int64_t  count      = 0;
        int64_t  page_offset = 0;
    };

    int64_t num_pages() const {
        return (num_rows + ROWS_PER_PAGE - 1) / ROWS_PER_PAGE;
    }

    int64_t page_for_row(int64_t row) const {
        return row / ROWS_PER_PAGE;
    }

    int64_t page_row_begin(int64_t page_idx) const {
        return page_idx * ROWS_PER_PAGE;
    }

    int64_t page_row_end(int64_t page_idx) const {
        return std::min(num_rows, page_row_begin(page_idx) + ROWS_PER_PAGE);
    }

    int64_t contiguous_rows(int64_t row_begin, int64_t max_rows) const {
        if (row_begin < 0 || row_begin > num_rows || max_rows < 0)
            throw std::out_of_range("ColumnHandle: invalid row range");
        if (row_begin == num_rows || max_rows == 0)
            return 0;
        int64_t page_idx = page_for_row(row_begin);
        return std::min(max_rows, page_row_end(page_idx) - row_begin);
    }

    // Pin one page; *count is set to the number of valid elements on this page.
    // Caller must call unpin_page(page_idx) when finished with the pointer.
    const T* pin_page(int64_t page_idx, int64_t* count = nullptr) const {
        if (!valid())
            throw std::runtime_error("ColumnHandle: invalid handle");
        if (page_idx < 0 || page_idx >= num_pages())
            throw std::out_of_range("ColumnHandle: invalid page index");
        int64_t byte_count;
        const uint8_t* raw = pool->pin_page(col_id, page_idx, &byte_count);
        if (count) *count = byte_count / static_cast<int64_t>(sizeof(T));
        return reinterpret_cast<const T*>(raw);
    }

    PinnedRange pin_range(int64_t row_begin, int64_t max_rows) const {
        int64_t count = contiguous_rows(row_begin, max_rows);
        if (count == 0)
            return PinnedRange{};
        int64_t page_idx = page_for_row(row_begin);
        int64_t page_count;
        const T* page = pin_page(page_idx, &page_count);
        int64_t offset = row_begin - page_row_begin(page_idx);
        if (offset + count > page_count) {
            unpin_page(page_idx);
            throw std::runtime_error("ColumnHandle: computed range exceeds pinned page");
        }
        return PinnedRange{page + offset, page_idx, row_begin, count, offset};
    }

    T get(int64_t row) const {
        if (row < 0 || row >= num_rows)
            throw std::out_of_range("ColumnHandle: invalid row index");
        auto range = pin_range(row, 1);
        T value = range.data[0];
        unpin_page(range.page_idx);
        return value;
    }

    void unpin_page(int64_t page_idx) const {
        if (!valid())
            throw std::runtime_error("ColumnHandle: invalid handle");
        pool->unpin_page(col_id, page_idx);
    }

    bool valid() const { return pool != nullptr && col_id >= 0; }
};

// Variable-length UTF-8/string column encoded as:
//   offsets: uint64_t[num_rows + 1], where offsets[i+1] - offsets[i] is row i length
//   bytes:   char[total_bytes], concatenated string payloads
//
// This is correct and simple. Hot query paths can later specialize common
// predicates by scanning offsets/bytes directly.
struct StringColumnHandle {
    ColumnHandle<uint64_t> offsets;
    ColumnHandle<char>     bytes;
    int64_t                num_rows = 0;

    bool valid() const {
        return offsets.valid() && bytes.valid() && num_rows >= 0;
    }

    std::string get(int64_t row) const {
        if (!valid())
            throw std::runtime_error("StringColumnHandle: invalid handle");
        if (row < 0 || row >= num_rows)
            throw std::out_of_range("StringColumnHandle: invalid row index");

        uint64_t begin = offsets.get(row);
        uint64_t end   = offsets.get(row + 1);
        if (end < begin)
            throw std::runtime_error("StringColumnHandle: offsets are not monotonic");

        std::string out;
        out.resize(static_cast<size_t>(end - begin));
        int64_t pos = static_cast<int64_t>(begin);
        int64_t remaining = static_cast<int64_t>(end - begin);
        int64_t written = 0;
        while (remaining > 0) {
            int64_t chunk = bytes.contiguous_rows(pos, remaining);
            auto range = bytes.pin_range(pos, chunk);
            std::copy(range.data, range.data + range.count, out.begin() + written);
            bytes.unpin_page(range.page_idx);
            pos += range.count;
            written += range.count;
            remaining -= range.count;
        }
        return out;
    }
};
