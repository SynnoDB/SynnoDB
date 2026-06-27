#include "loader_utils.hpp"

#include <algorithm>
#include <arrow/io/file.h>
#include <arrow/memory_pool.h>
#include <arrow/util/thread_pool.h>
#include <chrono>
#include <exception>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <parquet/arrow/reader.h>
#include <thread>
#include <vector>

// FILE_VERSION: 3

using Clock = std::chrono::steady_clock;

static std::unique_ptr<parquet::arrow::FileReader> OpenParquetReader(
    const std::shared_ptr<arrow::io::RandomAccessFile>& file) {
    parquet::ReaderProperties reader_props;
    reader_props.set_footer_read_size(8 * 1024 * 1024);

    parquet::ArrowReaderProperties arrow_props(/*use_threads=*/false);
    arrow_props.set_pre_buffer(true);

    auto cache_options = arrow::io::CacheOptions::Defaults();
    cache_options.hole_size_limit = 1 * 1024 * 1024;
    cache_options.range_size_limit = 128 * 1024 * 1024;
    cache_options.lazy = false;
    arrow_props.set_cache_options(cache_options);

    parquet::arrow::FileReaderBuilder builder;
    auto open_status = builder.Open(file, reader_props);
    if (!open_status.ok()) {
        std::cerr << "ERROR: FileReaderBuilder::Open: " << open_status.ToString()
                  << "\n";
        std::exit(1);
    }
    builder.properties(arrow_props);
    auto build_result = builder.Build();
    if (!build_result.ok()) {
        std::cerr << "ERROR: FileReaderBuilder::Build: "
                  << build_result.status().ToString() << "\n";
        std::exit(1);
    }
    return std::move(build_result).ValueOrDie();
}

static std::once_flag set_arrow_threads;

static void EnsureArrowThreads() {
    std::call_once(set_arrow_threads, [&] {
        const auto hw_threads = std::max(1u, std::thread::hardware_concurrency());
        if (arrow::SetCpuThreadPoolCapacity(static_cast<int>(hw_threads)).ok()) {
            auto pool = arrow::internal::GetCpuThreadPool();
            std::cerr << "Arrow CPU pool capacity: " << pool->GetCapacity() << "\n";
        }
    });
}

int NumParquetRowGroups(const std::string& path) {
    EnsureArrowThreads();
    auto file_result =
        arrow::io::MemoryMappedFile::Open(path, arrow::io::FileMode::READ);
    if (!file_result.ok()) {
        throw std::runtime_error("MemoryMappedFile::Open: " +
                                 file_result.status().ToString());
    }
    auto reader = OpenParquetReader(std::move(file_result).ValueOrDie());
    auto md = reader->parquet_reader()->metadata();
    if (!md)
        throw std::runtime_error("Failed to fetch Parquet metadata");
    return md->num_row_groups();
}

std::shared_ptr<arrow::Table> ReadParquetRowGroup(
    const std::string& path,
    int row_group,
    const std::vector<int>& column_indices) {
    EnsureArrowThreads();
    auto file_result =
        arrow::io::MemoryMappedFile::Open(path, arrow::io::FileMode::READ);
    if (!file_result.ok()) {
        throw std::runtime_error("MemoryMappedFile::Open: " +
                                 file_result.status().ToString());
    }
    auto reader = OpenParquetReader(std::move(file_result).ValueOrDie());
    std::shared_ptr<arrow::Table> table;
    arrow::Status st;
    if (column_indices.empty()) {
        st = reader->ReadRowGroup(row_group, &table);
    } else {
        st = reader->ReadRowGroup(row_group, column_indices, &table);
    }
    if (!st.ok())
        throw std::runtime_error("ReadRowGroup failed: " + st.ToString());
    return table;
}

std::shared_ptr<arrow::Table> ReadParquetTable(const std::string& path) {
    EnsureArrowThreads();

    const auto t0 = Clock::now();
    auto meta_file_result =
        arrow::io::MemoryMappedFile::Open(path, arrow::io::FileMode::READ);
    if (!meta_file_result.ok()) {
        std::cerr << "ERROR: MemoryMappedFile::Open(meta): "
                  << meta_file_result.status().ToString() << "\n";
        std::exit(1);
    }
    auto meta_file = std::move(meta_file_result).ValueOrDie();
    auto meta_reader = OpenParquetReader(meta_file);
    auto md = meta_reader->parquet_reader()->metadata();
    if (!md) {
        std::cerr << "ERROR: Failed to fetch Parquet metadata\n";
        std::exit(1);
    }
    const int num_rgs = md->num_row_groups();
    if (num_rgs <= 0) {
        std::shared_ptr<arrow::Table> table;
        auto read_status = meta_reader->ReadTable(&table);
        if (!read_status.ok()) {
            std::cerr << "ERROR: ReadTable(empty): " << read_status.ToString()
                      << "\n";
            std::exit(1);
        }
        const auto t1 = Clock::now();
        const auto ms =
            std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count();
        std::cerr << "Loaded " << path << " in " << ms << " ms\n";
        return table;
    }

    int nthreads = static_cast<int>(std::thread::hardware_concurrency());
    if (nthreads <= 0)
        nthreads = 1;
    nthreads = std::max(1, std::min(nthreads, num_rgs));

    std::vector<std::shared_ptr<arrow::Table>> worker_tables(nthreads);
    std::mutex ex_mu;
    std::exception_ptr ex_ptr = nullptr;

    // Open once and share across workers — one mmap per worker exhausts virtual
    // address space at large scale factors (96 threads × multi-GB file).
    auto shared_file_result =
        arrow::io::MemoryMappedFile::Open(path, arrow::io::FileMode::READ);
    if (!shared_file_result.ok()) {
        throw std::runtime_error("MemoryMappedFile::Open(worker): " +
                                 shared_file_result.status().ToString());
    }
    auto shared_file =
        std::static_pointer_cast<arrow::io::RandomAccessFile>(
            std::move(shared_file_result).ValueOrDie());

    std::vector<std::thread> workers;
    workers.reserve(nthreads);
    for (int t = 0; t < nthreads; ++t) {
        workers.emplace_back([&, t]() {
            try {
                auto reader = OpenParquetReader(shared_file);

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
                    throw std::runtime_error(
                        "ReadRowGroups failed: " + st.ToString());
                }
                worker_tables[t] = std::move(piece);
            } catch (...) {
                std::lock_guard<std::mutex> lk(ex_mu);
                if (!ex_ptr)
                    ex_ptr = std::current_exception();
            }
        });
    }

    for (auto& th : workers)
        th.join();
    if (ex_ptr)
        std::rethrow_exception(ex_ptr);

    std::vector<std::shared_ptr<arrow::Table>> nonnull;
    nonnull.reserve(worker_tables.size());
    for (auto& wt : worker_tables) {
        if (wt)
            nonnull.push_back(std::move(wt));
    }

    auto final_concat = arrow::ConcatenateTables(nonnull);
    if (!final_concat.ok()) {
        std::cerr << "ERROR: ConcatenateTables: "
                  << final_concat.status().ToString() << "\n";
        std::exit(1);
    }
    std::shared_ptr<arrow::Table> table = *final_concat;

    const auto t1 = Clock::now();
    const auto ms =
        std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count();
    std::cerr << "Loaded " << path << " in " << ms << " ms\n";
    return table;
}
