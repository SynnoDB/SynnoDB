#include "file_loader_utils.hpp"


#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <new>
#include <stdexcept>

#include <arrow/array/array_binary.h>
#include <arrow/array/array_decimal.h>
#include <arrow/array/array_primitive.h>
#include <arrow/buffer.h>
#include <arrow/io/file.h>
#include <arrow/type_fwd.h>
#include <parquet/file_reader.h>

#include <fcntl.h>
#include <unistd.h>

static constexpr std::size_t FDSTREAM_BUF = 32ULL * 1024 * 1024;
static constexpr std::size_t FDSTREAM_ALIGN = BP_PAGE_BYTES;
// Zero-filled buffer reused for inter-page and end-of-file padding writes.
// 512 B is enough that even very small page tails resolve in one f.write().
static const char ZERO_PAD_BYTES[512] = {};

static void free_aligned_buffer(void* ptr) {
    std::free(ptr);
}

ParquetFileScanner::ParquetFileScanner(const std::string& path) {
    auto file_result = arrow::io::ReadableFile::Open(path);
    if (!file_result.ok()) {
        throw std::runtime_error("ParquetFileScanner: cannot open " + path +
                                 ": " + file_result.status().ToString());
    }

    parquet::ArrowReaderProperties props;
    props.set_read_dictionary(0, false);

    auto parquet_reader = parquet::ParquetFileReader::Open(file_result.ValueOrDie());
    num_row_groups = parquet_reader->metadata()->num_row_groups();

    auto reader_result = parquet::arrow::FileReader::Make(
        arrow::default_memory_pool(), std::move(parquet_reader), props);
    if (!reader_result.ok()) {
        throw std::runtime_error(
            "ParquetFileScanner: FileReader::Make failed for " + path +
            ": " + reader_result.status().ToString());
    }

    reader = std::move(reader_result).ValueOrDie();
    reader->set_use_threads(true);
}

ParquetFileScanner::~ParquetFileScanner() = default;

std::shared_ptr<arrow::Table> ParquetFileScanner::read_row_group(
    int row_group,
    const std::vector<int>& column_indices) {
    std::shared_ptr<arrow::Table> table;
    arrow::Status status;
    if (column_indices.empty()) {
        status = reader->ReadRowGroup(row_group, &table);
    } else {
        status = reader->ReadRowGroup(row_group, column_indices, &table);
    }
    if (!status.ok()) {
        throw std::runtime_error("ParquetFileScanner: ReadRowGroup failed rg=" +
                                 std::to_string(row_group) + ": " +
                                 status.ToString());
    }
    return table;
}

std::string_view extract_sv(const arrow::Array* arr, int64_t i) {
    switch (arr->type_id()) {
        case arrow::Type::STRING:
            return static_cast<const arrow::StringArray*>(arr)->GetView(i);
        case arrow::Type::LARGE_STRING:
            return static_cast<const arrow::LargeStringArray*>(arr)->GetView(i);
        default:
            throw std::runtime_error("extract_sv: unexpected type " +
                                     arr->type()->ToString());
    }
}

int64_t extract_int64(const arrow::Array* arr, int64_t i) {
    using T = arrow::Type;
    switch (arr->type_id()) {
        case T::INT8:
            return static_cast<const arrow::Int8Array*>(arr)->Value(i);
        case T::INT16:
            return static_cast<const arrow::Int16Array*>(arr)->Value(i);
        case T::INT32:
            return static_cast<const arrow::Int32Array*>(arr)->Value(i);
        case T::INT64:
            return static_cast<const arrow::Int64Array*>(arr)->Value(i);
        case T::UINT8:
            return static_cast<int64_t>(
                static_cast<const arrow::UInt8Array*>(arr)->Value(i));
        case T::UINT16:
            return static_cast<int64_t>(
                static_cast<const arrow::UInt16Array*>(arr)->Value(i));
        case T::UINT32:
            return static_cast<int64_t>(
                static_cast<const arrow::UInt32Array*>(arr)->Value(i));
        case T::UINT64:
            return static_cast<int64_t>(
                static_cast<const arrow::UInt64Array*>(arr)->Value(i));
        case T::FLOAT:
            return static_cast<int64_t>(
                std::llround(static_cast<const arrow::FloatArray*>(arr)->Value(i) *
                             100.0));
        case T::DOUBLE:
            return static_cast<int64_t>(
                std::llround(static_cast<const arrow::DoubleArray*>(arr)->Value(i) *
                             100.0));
        case T::DECIMAL128: {
            const auto* da = static_cast<const arrow::Decimal128Array*>(arr);
            int64_t lo;
            std::memcpy(&lo, da->GetValue(i), sizeof(int64_t));
            return lo;
        }
        case T::DATE32:
            return static_cast<const arrow::Date32Array*>(arr)->Value(i);
        default:
            throw std::runtime_error("extract_int64: unsupported Arrow type " +
                                     arr->type()->ToString());
    }
}

int32_t extract_int32(const arrow::Array* arr, int64_t i) {
    return static_cast<int32_t>(extract_int64(arr, i));
}

FdStream::FdStream() : buf_(nullptr, free_aligned_buffer), cap_(FDSTREAM_BUF) {
    static_assert(FDSTREAM_BUF % FDSTREAM_ALIGN == 0);
    void* ptr = nullptr;
    if (posix_memalign(&ptr, FDSTREAM_ALIGN, FDSTREAM_BUF) != 0) {
        throw std::bad_alloc();
    }
    buf_.reset(static_cast<char*>(ptr));
}

FdStream::~FdStream() {
    if (fd_ >= 0) {
        flush_raw();
        ::close(fd_);
    }
}

void FdStream::open(const std::string& path) {
    fd_ = ::open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd_ < 0) {
        throw std::runtime_error("FdStream: cannot open " + path + ": " +
                                 std::strerror(errno));
    }
    pos_ = 0;
}

void FdStream::flush_raw() {
    if (pos_ == 0) return;

    const char* p = buf_.get();
    std::size_t remaining = pos_;
    while (remaining > 0) {
        ssize_t n = ::write(fd_, p, remaining);
        if (n < 0) {
            if (errno == EINTR) continue;
            throw std::runtime_error("FdStream: write error: " +
                                     std::string(std::strerror(errno)));
        }
        p += n;
        remaining -= static_cast<std::size_t>(n);
    }
    pos_ = 0;
}

void FdStream::write(const void* data, std::size_t n) {
    const char* src = reinterpret_cast<const char*>(data);
    while (n > 0) {
        std::size_t available = cap_ - pos_;
        if (available == 0) {
            flush_raw();
            available = cap_;
        }
        std::size_t chunk = std::min(n, available);
        std::memcpy(buf_.get() + pos_, src, chunk);
        pos_ += chunk;
        src += chunk;
        n -= chunk;
    }
}

void FdStream::close() {
    if (fd_ >= 0) {
        flush_raw();
        ::close(fd_);
        fd_ = -1;
        pos_ = 0;
    }
}

void write_int32_chunk_fast(FdStream& out, const arrow::Array* chunk) {
    using T = arrow::Type;
    int64_t len = chunk->length();
    if (len == 0) return;

    auto tid = chunk->type_id();
    if (tid == T::INT32 || tid == T::DATE32) {
        const int32_t* raw = reinterpret_cast<const int32_t*>(
            chunk->data()->buffers[1]->data()) + chunk->offset();
        out.write(raw, static_cast<std::size_t>(len) * sizeof(int32_t));
        return;
    }

    constexpr int64_t BATCH = 65536;
    int32_t tmp[BATCH];
    for (int64_t i = 0; i < len;) {
        int64_t n = std::min(BATCH, len - i);
        for (int64_t j = 0; j < n; ++j) {
            tmp[j] = extract_int32(chunk, i + j);
        }
        out.write(tmp, static_cast<std::size_t>(n) * sizeof(int32_t));
        i += n;
    }
}

void write_int64_chunk_fast(FdStream& out, const arrow::Array* chunk) {
    using T = arrow::Type;
    int64_t len = chunk->length();
    if (len == 0) return;

    auto tid = chunk->type_id();
    if (tid == T::INT64) {
        const int64_t* raw = reinterpret_cast<const int64_t*>(
            chunk->data()->buffers[1]->data()) + chunk->offset();
        out.write(raw, static_cast<std::size_t>(len) * sizeof(int64_t));
        return;
    }

    if (tid == T::DECIMAL128) {
        const auto* da = static_cast<const arrow::Decimal128Array*>(chunk);
        constexpr int64_t BATCH = 65536;
        int64_t tmp[BATCH];
        for (int64_t i = 0; i < len;) {
            int64_t n = std::min(BATCH, len - i);
            for (int64_t j = 0; j < n; ++j) {
                std::memcpy(&tmp[j], da->GetValue(i + j), sizeof(int64_t));
            }
            out.write(tmp, static_cast<std::size_t>(n) * sizeof(int64_t));
            i += n;
        }
        return;
    }

    constexpr int64_t BATCH = 65536;
    int64_t tmp[BATCH];
    for (int64_t i = 0; i < len;) {
        int64_t n = std::min(BATCH, len - i);
        for (int64_t j = 0; j < n; ++j) {
            tmp[j] = extract_int64(chunk, i + j);
        }
        out.write(tmp, static_cast<std::size_t>(n) * sizeof(int64_t));
        i += n;
    }
}

void StringWriter::open(const std::string& offsets_path,
                        const std::string& bytes_path) {
    off_f_.open(offsets_path);
    byte_f_.open(bytes_path);
    running_ = 0;
    nrows_ = 0;
    off_n_ = 0;
}

void StringWriter::flush_offsets() {
    if (off_n_ == 0) return;
    off_f_.write(off_buf_, off_n_ * sizeof(uint64_t));
    off_n_ = 0;
}

void StringWriter::push_offset() {
    off_buf_[off_n_++] = running_;
    if (off_n_ == OFF_BATCH) flush_offsets();
}

void StringWriter::write_chunk(const arrow::Array* arr) {
    int64_t len = arr->length();
    if (len == 0) return;

    auto tid = arr->type_id();
    if (tid == arrow::Type::STRING) {
        const int32_t* ao =
            reinterpret_cast<const int32_t*>(arr->data()->buffers[1]->data()) +
            arr->offset();
        int32_t begin = ao[0];
        int32_t end = ao[len];
        write_offsets_from_arrow_offsets(ao, len);
        if (end > begin) {
            byte_f_.write(arr->data()->buffers[2]->data() + begin,
                          static_cast<std::size_t>(end - begin));
        }
        nrows_ += len;
        return;
    }

    if (tid == arrow::Type::LARGE_STRING) {
        const int64_t* ao =
            reinterpret_cast<const int64_t*>(arr->data()->buffers[1]->data()) +
            arr->offset();
        int64_t begin = ao[0];
        int64_t end = ao[len];
        write_offsets_from_arrow_offsets(ao, len);
        if (end > begin) {
            byte_f_.write(arr->data()->buffers[2]->data() + begin,
                          static_cast<std::size_t>(end - begin));
        }
        nrows_ += len;
        return;
    }

    for (int64_t i = 0; i < len; ++i) {
        auto sv = extract_sv(arr, i);
        push_offset();
        if (!sv.empty()) {
            byte_f_.write(sv.data(), sv.size());
        }
        running_ += sv.size();
    }
    nrows_ += len;
}

void StringWriter::finish() {
    flush_offsets();
    off_f_.write(&running_, sizeof(uint64_t));
}

ColumnHandle<int32_t> reg_int32(BufferPool* pool,
                                const std::string& path,
                                int64_t num_rows) {
    return reg_fixed_width<int32_t>(pool, path, num_rows);
}

ColumnHandle<int64_t> reg_int64(BufferPool* pool,
                                const std::string& path,
                                int64_t num_rows) {
    return reg_fixed_width<int64_t>(pool, path, num_rows);
}

StringColumnHandle reg_string(BufferPool* pool,
                              const std::string& offsets_path,
                              const std::string& bytes_path,
                              int64_t num_rows) {
    int64_t off_bytes = (num_rows + 1) * static_cast<int64_t>(sizeof(uint64_t));
    int64_t byte_bytes = static_cast<int64_t>(std::filesystem::file_size(bytes_path));
    int off_cid = pool->register_column(offsets_path, off_bytes);
    int byte_cid = pool->register_column(bytes_path, byte_bytes == 0 ? 1 : byte_bytes);

    StringColumnHandle sc;
    sc.offsets = ColumnHandle<uint64_t>{pool, off_cid, num_rows + 1};
    sc.bytes = ColumnHandle<char>{pool, byte_cid, byte_bytes == 0 ? 1 : byte_bytes};
    sc.num_rows = num_rows;
    return sc;
}

bool all_exist(const std::vector<std::string>& paths) {
    for (const auto& path : paths) {
        if (!std::filesystem::exists(path)) return false;
    }
    return true;
}

void write_col_int32(FdStream& f, const arrow::ChunkedArray* ca) {
    for (int c = 0; c < ca->num_chunks(); ++c) {
        write_int32_chunk_fast(f, ca->chunk(c).get());
    }
}

void write_col_int64(FdStream& f, const arrow::ChunkedArray* ca) {
    for (int c = 0; c < ca->num_chunks(); ++c) {
        write_int64_chunk_fast(f, ca->chunk(c).get());
    }
}

void write_col_string(StringWriter& sw, const arrow::ChunkedArray* ca) {
    for (int c = 0; c < ca->num_chunks(); ++c) {
        sw.write_chunk(ca->chunk(c).get());
    }
}

// Append `pad_bytes` zeroes to `f` using the shared ZERO_PAD_BYTES scratch.
static void write_zero_pad(FdStream& f, int64_t pad_bytes) {
    while (pad_bytes > 0) {
        int64_t n = std::min<int64_t>(pad_bytes, sizeof(ZERO_PAD_BYTES));
        f.write(ZERO_PAD_BYTES, static_cast<std::size_t>(n));
        pad_bytes -= n;
    }
}

// Append rows from `ca` to `f` in the page-aligned fixed-char layout consumed
// by reg_page_aligned_fixed_width<std::array<char,width>>. See the helper's
// header comment for the exact on-disk layout.
//
// Invariant maintained across calls: after each call returns, the file's byte
// position is either on a BP_PAGE_BYTES boundary (if rows_written is a
// multiple of rows_per_page) or at byte (page_pos * width) within the current
// page. The per-iteration clamp `n <= rows_per_page - page_pos` guarantees a
// single f.write() never crosses a page boundary, and the tail-padding write
// fires exactly when a page is completed. Short strings are right-zero-padded
// (memset of the row batch to 0 before memcpy); strings longer than `width`
// are silently truncated.
void write_fixed_char_col_aligned(FdStream& f,
                                  const arrow::ChunkedArray* ca,
                                  int width,
                                  int64_t& rows_written) {
    if (width <= 0 || width > BP_PAGE_BYTES) {
        throw std::invalid_argument("write_fixed_char_col_aligned: invalid width");
    }

    const int64_t rows_per_page = BP_PAGE_BYTES / width;
    // Bytes left over per page after rows_per_page rows of `width`-byte data.
    // Zero only when width divides BP_PAGE_BYTES; non-zero is the whole point
    // of this helper.
    const int64_t tail_bytes = BP_PAGE_BYTES - rows_per_page * width;

    constexpr int64_t RBATCH = 4096;
    std::vector<char> row_buf(static_cast<std::size_t>(width) * RBATCH, '\0');
    char* base = row_buf.data();

    for (int c = 0; c < ca->num_chunks(); ++c) {
        const arrow::Array* chunk = ca->chunk(c).get();
        int64_t len = chunk->length();
        for (int64_t i = 0; i < len;) {
            int64_t n = std::min<int64_t>(RBATCH, len - i);
            int64_t page_pos = rows_written % rows_per_page;
            // Never cross a page boundary in a single write — keeps the page
            // tail-pad write below at the right offset.
            n = std::min<int64_t>(n, rows_per_page - page_pos);

            // Zero the whole batch first so unused tail bytes of short rows
            // are deterministic.
            std::memset(base, 0, static_cast<std::size_t>(width) *
                                   static_cast<std::size_t>(n));
            for (int64_t j = 0; j < n; ++j) {
                auto sv = extract_sv(chunk, i + j);
                int64_t copy_len =
                    std::min<int64_t>(static_cast<int64_t>(sv.size()), width);
                if (copy_len > 0) {
                    std::memcpy(base + j * width, sv.data(),
                                static_cast<std::size_t>(copy_len));
                }
            }

            f.write(base, static_cast<std::size_t>(width) *
                          static_cast<std::size_t>(n));
            rows_written += n;
            i += n;

            // Just completed a page → emit its trailing pad so the next page
            // starts at a BP_PAGE_BYTES-aligned file offset.
            if (tail_bytes > 0 && rows_written % rows_per_page == 0) {
                write_zero_pad(f, tail_bytes);
            }
        }
    }
}

// Pad the file's last (partial) page so the file size matches what
// reg_page_aligned_fixed_width<std::array<char,width>>(num_rows=rows_written)
// will register: ceil(rows_written / rows_per_page) * BP_PAGE_BYTES.
//
// Must be called exactly once per file, after the final
// write_fixed_char_col_aligned call. No-op cases:
//   - rows_written == 0 (empty input → zero-byte file is what the registrar
//     expects, so writing tail_bytes here would corrupt the size invariant)
//   - rows_written is a multiple of rows_per_page (the loop above already
//     emitted the inter-page pad for the just-completed page)
// Both cases are caught by `page_pos == 0`.
void finish_fixed_char_col_aligned(FdStream& f, int64_t rows_written, int width) {
    if (width <= 0 || width > BP_PAGE_BYTES) {
        throw std::invalid_argument("finish_fixed_char_col_aligned: invalid width");
    }

    const int64_t rows_per_page = BP_PAGE_BYTES / width;
    const int64_t tail_bytes = BP_PAGE_BYTES - rows_per_page * width;
    int64_t page_pos = rows_written % rows_per_page;
    if (page_pos == 0) return;

    int64_t remaining_rows = rows_per_page - page_pos;
    write_zero_pad(f, remaining_rows * width + tail_bytes);
}
