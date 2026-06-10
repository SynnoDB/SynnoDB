#include <arrow/io/file.h>
#include <arrow/io/caching.h>
#include <arrow/result.h>
#include <arrow/status.h>
#include <arrow/array.h>
#include <arrow/table.h>
#include <arrow/util/thread_pool.h>

#include <parquet/arrow/reader.h>

#include <chrono>
#include <exception>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

using Clock = std::chrono::steady_clock;

static inline double ms_since(const Clock::time_point &t0) {
  return std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
}

static void Die(const std::string &msg) {
  std::cerr << "ERROR: " << msg << "\n";
  std::exit(1);
}

static void Check(const arrow::Status &st, const std::string &what) {
  if (!st.ok())
    Die(what + ": " + st.ToString());
}

template <class T>
static T Unwrap(arrow::Result<T> &&r, const std::string &what) {
  if (!r.ok())
    Die(what + ": " + r.status().ToString());
  return std::move(r).ValueOrDie();
}

static int ParseInt(const char *s, int fallback) {
  if (!s)
    return fallback;
  try {
    return std::max(1, std::stoi(s));
  } catch (...) {
    return fallback;
  }
}

static int64_t SumBuffers(const std::shared_ptr<arrow::ArrayData> &data) {
  if (!data)
    return 0;
  int64_t bytes = 0;
  for (const auto &buf : data->buffers) {
    if (buf)
      bytes += buf->size();
  }
  if (data->dictionary)
    bytes += SumBuffers(data->dictionary);
  for (const auto &child : data->child_data)
    bytes += SumBuffers(child);
  return bytes;
}

static int64_t ApproxTableBufferBytes(const std::shared_ptr<arrow::Table> &table) {
  if (!table)
    return 0;
  int64_t bytes = 0;
  const int ncols = table->num_columns();
  for (int c = 0; c < ncols; ++c) {
    const auto &col = table->column(c);
    const int nchunks = col->num_chunks();
    for (int i = 0; i < nchunks; ++i) {
      bytes += SumBuffers(col->chunk(i)->data());
    }
  }
  return bytes;
}

static std::unique_ptr<parquet::arrow::FileReader> OpenParquetReader(
    const std::shared_ptr<arrow::io::RandomAccessFile> &file,
    const std::string &what) {
  parquet::ReaderProperties reader_props;
  // Larger footer read to reduce NFS round-trips for large metadata.
  reader_props.set_footer_read_size(8 * 1024 * 1024);

  parquet::ArrowReaderProperties arrow_props(/*use_threads=*/true);
  arrow_props.set_pre_buffer(true);

  auto cache_options = arrow::io::CacheOptions::Defaults();
  cache_options.hole_size_limit = 1 * 1024 * 1024;
  cache_options.range_size_limit = 128 * 1024 * 1024;
  cache_options.lazy = false;
  arrow_props.set_cache_options(cache_options);

  parquet::arrow::FileReaderBuilder builder;
  Check(builder.Open(file, reader_props), what + ": FileReaderBuilder::Open");
  builder.properties(arrow_props);
  return Unwrap(builder.Build(), what + ": FileReaderBuilder::Build");
}

static std::shared_ptr<arrow::Table> ReadParquetTable(
    const std::string &path, int nthreads, int64_t *out_total_rows,
    double *out_meta_ms, double *out_read_ms, double *out_concat_ms,
    double *out_total_ms) {
  const auto t_total0 = Clock::now();

  // Open once to get metadata (row group count).
  const auto t_meta0 = Clock::now();
  auto infile0 =
      Unwrap(arrow::io::MemoryMappedFile::Open(path, arrow::io::FileMode::READ),
             "MemoryMappedFile::Open(meta)");
  auto reader0 = OpenParquetReader(infile0, "OpenReader(meta)");

  auto md = reader0->parquet_reader()->metadata();
  if (!md)
    Die("Failed to fetch Parquet metadata");
  const int num_rgs = md->num_row_groups();
  const int num_cols = md->num_columns();

  if (out_meta_ms)
    *out_meta_ms = ms_since(t_meta0);

  std::cerr << "Row groups: " << num_rgs << ", columns: " << num_cols << "\n";
  std::cerr << "Metadata/open time: " << std::fixed << std::setprecision(3)
            << (out_meta_ms ? *out_meta_ms : ms_since(t_meta0)) << " ms\n";

  if (num_rgs <= 0) {
    // Edge case: empty file
    std::shared_ptr<arrow::Table> table;
    const auto t_read0 = Clock::now();
    Check(reader0->ReadTable(&table), "ReadTable(empty)");
    if (out_read_ms)
      *out_read_ms = ms_since(t_read0);
    if (out_total_ms)
      *out_total_ms = ms_since(t_total0);
    if (out_total_rows)
      *out_total_rows = table ? table->num_rows() : 0;
    return table;
  }

  // Don't use more threads than row groups (no point).
  nthreads = std::max(1, std::min(nthreads, num_rgs));
  std::cerr << "Using worker threads: " << nthreads << "\n";

  std::vector<std::shared_ptr<arrow::Table>> worker_tables(nthreads);

  std::mutex ex_mu;
  std::exception_ptr ex_ptr = nullptr;

  const auto t_read0 = Clock::now();

  // Worker threads: each reads a contiguous range of row groups to improve IO locality.
  std::vector<std::thread> workers;
  workers.reserve(nthreads);

  for (int t = 0; t < nthreads; ++t) {
    workers.emplace_back([&, t]() {
      try {
        auto infile = Unwrap(
            arrow::io::MemoryMappedFile::Open(path, arrow::io::FileMode::READ),
            "MemoryMappedFile::Open(worker)");
        auto reader = OpenParquetReader(infile, "OpenReader(worker)");

        const int rgs_per_thread = (num_rgs + nthreads - 1) / nthreads;
        const int rg_begin = t * rgs_per_thread;
        const int rg_end = std::min(num_rgs, rg_begin + rgs_per_thread);
        if (rg_begin >= num_rgs) {
          worker_tables[t] = nullptr;
          return;
        }

        std::vector<int> row_groups;
        row_groups.reserve(rg_end - rg_begin);
        for (int rg = rg_begin; rg < rg_end; ++rg)
          row_groups.push_back(rg);

        std::shared_ptr<arrow::Table> piece;
        auto st = reader->ReadRowGroups(row_groups, &piece);
        if (!st.ok()) {
          throw std::runtime_error("ReadRowGroups(" +
                                   std::to_string(rg_begin) + "-" +
                                   std::to_string(rg_end - 1) +
                                   ") failed: " + st.ToString());
        }
        worker_tables[t] = std::move(piece);
      } catch (...) {
        std::lock_guard<std::mutex> lk(ex_mu);
        if (!ex_ptr)
          ex_ptr = std::current_exception();
      }
    });
  }

  for (auto &th : workers)
    th.join();
  if (ex_ptr)
    std::rethrow_exception(ex_ptr);

  if (out_read_ms)
    *out_read_ms = ms_since(t_read0);

  // Final concat
  const auto t_concat0 = Clock::now();
  std::vector<std::shared_ptr<arrow::Table>> nonnull;
  nonnull.reserve(worker_tables.size());
  int64_t total_rows = 0;
  for (auto &wt : worker_tables) {
    if (wt) {
      total_rows += wt->num_rows();
      nonnull.push_back(std::move(wt));
    }
  }

  auto final_concat = arrow::ConcatenateTables(nonnull);
  Check(final_concat.status(), "ConcatenateTables(final)");
  std::shared_ptr<arrow::Table> table = *final_concat;

  if (out_concat_ms)
    *out_concat_ms = ms_since(t_concat0);
  if (out_total_ms)
    *out_total_ms = ms_since(t_total0);
  if (out_total_rows)
    *out_total_rows = total_rows;
  return table;
}

int main(int argc, char **argv) {
  if (argc < 2) {
    std::cerr << "Usage: " << argv[0] << " <file.parquet> [threads]\n";
    return 2;
  }
  const std::string path = argv[1];

  int hw = static_cast<int>(std::thread::hardware_concurrency());
  if (hw <= 0)
    hw = 1;
  int nthreads = (argc >= 3) ? ParseInt(argv[2], hw) : hw;

  // Ensure Arrow's CPU pool is large (reader->set_use_threads uses it).
  {
    auto st = arrow::SetCpuThreadPoolCapacity(nthreads);
    Check(st, "arrow::SetCpuThreadPoolCapacity");
    auto *pool = arrow::internal::GetCpuThreadPool();
    std::cerr << "Arrow CPU pool capacity: " << pool->GetCapacity()
              << " (requested " << nthreads << ")\n";
  }

  int64_t total_rows = 0;
  double read_ms = 0.0;
  double concat_ms = 0.0;
  double total_ms = 0.0;
  std::shared_ptr<arrow::Table> table =
      ReadParquetTable(path, nthreads, &total_rows, nullptr, &read_ms,
                       &concat_ms, &total_ms);

  // Estimate in-memory size (approx; buffers may share / overhead not counted)
  int64_t bytes = 0;
  if (table) {
    bytes = ApproxTableBufferBytes(table);
  }

  std::cerr << "\n=== Timings ===\n";
  std::cerr << "Parallel read (row groups) time: " << read_ms << " ms\n";
  std::cerr << "Final concatenate time:         " << concat_ms << " ms\n";
  std::cerr << "Total time:                    " << total_ms << " ms\n";
  std::cerr << "\n=== Result ===\n";
  std::cerr << "Rows: " << total_rows << "\n";
  std::cerr << "Columns: " << (table ? table->num_columns() : 0) << "\n";
  std::cerr << "Approx buffer bytes: " << bytes << "\n";

  // Keep table alive to ensure "read into memory completely" isn't optimized
  // away. (Nothing else to do.)
  return 0;
}
