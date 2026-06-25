#pragma once

#include <algorithm>
#include <condition_variable>
#include <cstdint>
#include <cerrno>
#include <cstddef>
#include <fcntl.h>
#include <functional>
#include <mutex>
#include <sys/mman.h>
#include <stdexcept>
#include <string>
#include <system_error>
#include <unistd.h>
#include <unordered_map>
#include <vector>

#include "trace.hpp"

// Fixed-size page buffer pool for SSD-backed columnar storage.
//
// Column files are opened read-only and registered once with register_column().
// Pages are loaded on demand via pread and evicted using the CLOCK algorithm.
// Pinned pages (pin_count > 0) are never evicted.
//
// Typical usage:
//   BufferPool pool(num_frames);
//   int cid = pool.register_column("/path/col.bin", num_bytes);
//   int64_t n; const uint8_t* p = pool.pin_page(cid, page_idx, &n);
//   // ... use p[0..n-1] ...
//   pool.unpin_page(cid, page_idx);

static constexpr int64_t BP_PAGE_BYTES = 2LL * 1024 * 1024;  // 2 MiB pages

struct BufferPool {
    struct Frame {
        uint8_t* data      = nullptr;
        int      col_id    = -1;
        int64_t  page_idx  = -1;
        int      pin_count = 0;
        bool     clock_bit = false;
        bool     loading   = false;
    };

    struct ColInfo {
        int     fd        = -1;
        int64_t num_bytes = 0;
        int64_t num_pages = 0;
    };

    struct PageKey {
        int     col_id   = -1;
        int64_t page_idx = -1;

        bool operator==(const PageKey& other) const {
            return col_id == other.col_id && page_idx == other.page_idx;
        }
    };

    struct PageKeyHash {
        std::size_t operator()(const PageKey& k) const {
            std::size_t h1 = std::hash<int>{}(k.col_id);
            std::size_t h2 = std::hash<int64_t>{}(k.page_idx);
            return h1 ^ (h2 + 0x9e3779b97f4a7c15ULL + (h1 << 6) + (h1 >> 2));
        }
    };

    std::vector<Frame>                    frames_;
    std::vector<ColInfo>                  cols_;
    std::unordered_map<PageKey, int32_t, PageKeyHash> page_map_;
    std::mutex                            mu_;
    std::condition_variable               load_cv_;
    int32_t                               clock_hand_ = 0;

    explicit BufferPool(int64_t num_frames) {
        if (num_frames <= 0)
            throw std::invalid_argument("BufferPool: num_frames must be positive");
        frames_.resize(static_cast<size_t>(num_frames));
        for (auto& f : frames_)
            f.data = new uint8_t[BP_PAGE_BYTES];
        page_map_.reserve(static_cast<size_t>(num_frames) * 2);
    }

    ~BufferPool() {
        for (auto& f : frames_) delete[] f.data;
        for (auto& c : cols_)   if (c.fd >= 0) ::close(c.fd);
    }

    // Open a column file and return its column-id.
    // num_bytes must equal the file size in bytes.
    int register_column(const std::string& path, int64_t num_bytes) {
        if (num_bytes < 0)
            throw std::invalid_argument("BufferPool: negative column size for " + path);
        int fd = ::open(path.c_str(), O_RDONLY);
        if (fd < 0)
            throw std::system_error(errno, std::generic_category(), "BufferPool: cannot open " + path);
        // Sequential scans dominate this storage backend; this is only a hint.
        (void)::posix_fadvise(fd, 0, 0, POSIX_FADV_SEQUENTIAL);
        int64_t pages = (num_bytes + BP_PAGE_BYTES - 1) / BP_PAGE_BYTES;
        int id = static_cast<int>(cols_.size());
        cols_.push_back({fd, num_bytes, pages});
        return id;
    }

    // Pin a page into a frame and return a pointer to its raw bytes.
    // *bytes_out is set to the number of valid bytes in the page (may be < BP_PAGE_BYTES
    // for the last page of a column). Must call unpin_page() when done.
    const uint8_t* pin_page(int col_id, int64_t page_idx, int64_t* bytes_out = nullptr) {
        PROFILE_SCOPE("buffer_pool_pin_page");
        std::unique_lock<std::mutex> lk(mu_);
        validate_page_locked(col_id, page_idx);
        PageKey key{col_id, page_idx};

        auto it = page_map_.find(key);
        if (it != page_map_.end()) {
            Frame& f = frames_[it->second];
            ++f.pin_count;
            f.clock_bit = true;
            TRACE_ACCUM("buffer_pool_page_hits", 1);
            while (f.loading) {
                PROFILE_SCOPE("buffer_pool_wait_inflight_page");
                load_cv_.wait(lk, [&] { return !f.loading; });
            }
            if (bytes_out) *bytes_out = page_bytes_locked(col_id, page_idx);
            return f.data;
        }

        int victim = evict_locked();
        Frame& f = frames_[victim];
        int64_t off   = page_idx * BP_PAGE_BYTES;
        int64_t bread = page_bytes_locked(col_id, page_idx);

        f.col_id    = col_id;
        f.page_idx  = page_idx;
        f.pin_count = 1;
        f.clock_bit = true;
        f.loading   = true;
        page_map_[key] = victim;

        TRACE_ACCUM("buffer_pool_page_misses", 1);
        TRACE_ACCUM("buffer_pool_bytes_read", bread);

        lk.unlock();
        try {
            PROFILE_SCOPE("buffer_pool_read_page");
            read_exact_unlocked(cols_[col_id].fd, f.data, bread, off, col_id);
        } catch (...) {
            lk.lock();
            page_map_.erase(key);
            f.col_id    = -1;
            f.page_idx  = -1;
            f.pin_count = 0;
            f.clock_bit = false;
            f.loading   = false;
            lk.unlock();
            load_cv_.notify_all();
            throw;
        }

        lk.lock();
        f.loading = false;
        lk.unlock();
        load_cv_.notify_all();
        if (bytes_out) *bytes_out = bread;
        return f.data;
    }

    void unpin_page(int col_id, int64_t page_idx) {
        std::lock_guard<std::mutex> lk(mu_);
        validate_page_locked(col_id, page_idx);
        PageKey key{col_id, page_idx};
        auto it = page_map_.find(key);
        if (it == page_map_.end())
            throw std::runtime_error("BufferPool: unpin of page that is not pinned");
        Frame& f = frames_[it->second];
        if (f.pin_count <= 0)
            throw std::runtime_error("BufferPool: unpin would make pin count negative");
        --f.pin_count;
    }

    int64_t col_num_pages(int col_id)  const { return cols_[col_id].num_pages; }
    int64_t col_num_bytes(int col_id)  const { return cols_[col_id].num_bytes; }

    // ── Sequential-read bypass ────────────────────────────────────────────────
    // Read the entire column col_id into dst[0..col_num_bytes(col_id)) without
    // going through the frame allocator or lock.
    // dst must be at least col_num_bytes(col_id) bytes large.
    // Uses large pread() calls to maximise throughput from OS page cache or SSD.
    // Useful for large columns always accessed sequentially (e.g. string bytes).
    void read_col_sequential(int col_id, void* dst) const {
        if (col_id < 0 || col_id >= static_cast<int>(cols_.size()))
            throw std::out_of_range("BufferPool::read_col_sequential: invalid col_id");
        const ColInfo& ci = cols_[col_id];
        int64_t total = ci.num_bytes;
        if (total == 0) return;
        uint8_t* d = static_cast<uint8_t*>(dst);
        int64_t done = 0;
        while (done < total) {
            ssize_t n = ::pread(ci.fd, d + done,
                                static_cast<size_t>(total - done), done);
            if (n < 0) {
                if (errno == EINTR) continue;
                throw std::system_error(errno, std::generic_category(),
                                        "BufferPool::read_col_sequential: pread error");
            }
            if (n == 0)
                throw std::runtime_error("BufferPool::read_col_sequential: unexpected EOF");
            done += static_cast<int64_t>(n);
        }
    }

    // mmap the column file (read-only, shared mapping). Returns a pointer to
    // the mapped region of size col_num_bytes(col_id). Caller must call
    // munmap_col(col_id, ptr) when done.
    //
    // Always advises MADV_HUGEPAGE (THP must be "madvise" or "always" on the
    // host to take effect). access_advice is passed to madvise to hint the
    // access pattern — default is MADV_SEQUENTIAL; pass MADV_RANDOM for
    // sparse/random touches to disable kernel read-ahead.
    //
    // Process-wide RAM is bounded by RLIMIT_AS (set by the parent runner):
    // mmap consumes virtual address space, so a mapping that would push VSZ
    // past the limit fails with ENOMEM, surfaced here as a runtime_error
    // naming the column and size for attribution.
    //
    // Note: mmap'd regions live outside the frame pool — they are NOT released
    // by clear() and do not count against num_frames. Caller owns the lifetime.
    void* mmap_col(int col_id, int access_advice = MADV_SEQUENTIAL) const {
        if (col_id < 0 || col_id >= static_cast<int>(cols_.size()))
            throw std::out_of_range("BufferPool::mmap_col: invalid col_id");
        const ColInfo& ci = cols_[col_id];
        if (ci.num_bytes == 0) return nullptr;
        int64_t bytes = ci.num_bytes;
        void* ptr = ::mmap(nullptr, static_cast<size_t>(bytes),
                           PROT_READ, MAP_SHARED, ci.fd, 0);
        if (ptr == MAP_FAILED) {
            int err = errno;
            if (err == ENOMEM)
                throw std::runtime_error(
                    "BufferPool::mmap_col: ENOMEM mapping col " +
                    std::to_string(col_id) + " (" + std::to_string(bytes) +
                    " bytes); likely exceeded process RLIMIT_AS memory budget");
            throw std::system_error(err, std::generic_category(),
                                    "BufferPool::mmap_col: mmap failed for col " +
                                    std::to_string(col_id));
        }
        ::madvise(ptr, static_cast<size_t>(bytes), MADV_HUGEPAGE);
        ::madvise(ptr, static_cast<size_t>(bytes), access_advice);
        return ptr;
    }

    void munmap_col(int col_id, void* ptr) const {
        if (!ptr) return;
        const ColInfo& ci = cols_[col_id];
        ::munmap(ptr, static_cast<size_t>(ci.num_bytes));
    }

    // Evict all pages (including pinned ones) and reset all frame metadata.
    void clear() {
        std::lock_guard<std::mutex> lk(mu_);
        page_map_.clear();
        clock_hand_ = 0;
        for (auto& f : frames_) {
            f.col_id    = -1;
            f.page_idx  = -1;
            f.pin_count = 0;
            f.clock_bit = false;
            f.loading   = false;
        }
        load_cv_.notify_all();
    }

private:
    void validate_col_locked(int col_id) const {
        if (col_id < 0 || col_id >= static_cast<int>(cols_.size()))
            throw std::out_of_range("BufferPool: invalid column id " + std::to_string(col_id));
    }

    void validate_page_locked(int col_id, int64_t page_idx) const {
        validate_col_locked(col_id);
        if (page_idx < 0 || page_idx >= cols_[col_id].num_pages)
            throw std::out_of_range("BufferPool: invalid page " + std::to_string(page_idx) +
                                    " for column " + std::to_string(col_id));
    }

    int64_t page_bytes_locked(int col_id, int64_t page_idx) const {
        return std::min(BP_PAGE_BYTES, cols_[col_id].num_bytes - page_idx * BP_PAGE_BYTES);
    }

    void read_exact_unlocked(int fd, uint8_t* dst, int64_t bytes, int64_t off, int col_id) {
        int64_t done = 0;
        while (done < bytes) {
            ssize_t n = ::pread(fd, dst + done, static_cast<size_t>(bytes - done), off + done);
            if (n < 0) {
                if (errno == EINTR) continue;
                throw std::system_error(errno, std::generic_category(),
                                        "BufferPool: pread error on col " + std::to_string(col_id));
            }
            if (n == 0)
                throw std::runtime_error("BufferPool: unexpected EOF on col " + std::to_string(col_id));
            done += static_cast<int64_t>(n);
        }
    }

    int evict_locked() {
        int n = static_cast<int>(frames_.size());
        for (int tries = 0; tries < 2 * n; ++tries) {
            Frame& f = frames_[clock_hand_];
            if (f.pin_count == 0) {
                if (!f.clock_bit) {
                    if (f.col_id >= 0) {
                        page_map_.erase(PageKey{f.col_id, f.page_idx});
                        TRACE_ACCUM("buffer_pool_evictions", 1);
                    }
                    int v = clock_hand_;
                    clock_hand_ = (clock_hand_ + 1) % n;
                    return v;
                }
                f.clock_bit = false;
            }
            clock_hand_ = (clock_hand_ + 1) % n;
        }
        throw std::runtime_error("BufferPool: all frames pinned — increase pool size");
    }
};
