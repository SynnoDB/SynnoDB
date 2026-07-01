#include "thread_pool.hpp"

#include <array>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#define CHECK(cond, msg) do { if (!(cond)) throw std::runtime_error(msg); } while (0)

int main() {
    try {
        ThreadPool serial;
        serial.init(1, {});

        const int64_t expected_sum = (999 * 1000) / 2;
        auto sum_serial = parallel_reduce<int64_t>(
            serial, 1000, 0,
            [](int64_t& acc, int64_t i) { acc += i; },
            [](int64_t& acc, const int64_t& part) { acc += part; });
        CHECK(sum_serial == expected_sum, "serial parallel_reduce sum mismatch");

        ThreadPool pool;
        pool.init(4, {});

        auto sum_parallel = parallel_reduce<int64_t>(
            pool, 1000, 0,
            [](int64_t& acc, int64_t i) { acc += i; },
            [](int64_t& acc, const int64_t& part) { acc += part; });
        CHECK(sum_parallel == expected_sum, "parallel parallel_reduce sum mismatch");

        using Buckets = std::array<int64_t, 4>;
        auto buckets = parallel_reduce<Buckets>(
            pool, 1000, Buckets{0, 0, 0, 0},
            [](Buckets& acc, int64_t i) { acc[(size_t)(i & 3)] += 1; },
            [](Buckets& acc, const Buckets& part) {
                for (size_t i = 0; i < acc.size(); ++i) acc[i] += part[i];
            });
        CHECK((buckets == Buckets{250, 250, 250, 250}), "bucket reduction mismatch");

        auto ordered = parallel_reduce<std::vector<int>>(
            pool, 128, {},
            [](std::vector<int>& acc, int64_t i) { acc.push_back((int)i); },
            [](std::vector<int>& acc, const std::vector<int>& part) {
                acc.insert(acc.end(), part.begin(), part.end());
            });
        CHECK(ordered.size() == 128, "ordered projection size mismatch");
        for (int i = 0; i < 128; ++i) {
            CHECK(ordered[(size_t)i] == i, "ordered projection order mismatch");
        }

        bool reduce_threw = false;
        try {
            (void)parallel_reduce<int>(
                pool, 64, 0,
                [](int& acc, int64_t i) {
                    if (i == 17) throw std::runtime_error("reduce boom");
                    acc += 1;
                },
                [](int& acc, const int& part) { acc += part; });
        } catch (const std::runtime_error& e) {
            reduce_threw = std::string(e.what()).find("reduce boom") != std::string::npos;
        }
        CHECK(reduce_threw, "parallel_reduce did not propagate worker exception");

        bool for_threw = false;
        try {
            pool.parallel_for([](int tid, int) {
                if (tid == 2) throw std::runtime_error("for boom");
            });
        } catch (const std::runtime_error& e) {
            for_threw = std::string(e.what()).find("for boom") != std::string::npos;
        }
        CHECK(for_threw, "parallel_for did not propagate worker exception");

        auto after_exception = parallel_reduce<int64_t>(
            pool, 10, 0,
            [](int64_t& acc, int64_t i) { acc += i; },
            [](int64_t& acc, const int64_t& part) { acc += part; });
        CHECK(after_exception == 45, "pool was not reusable after exception");

        std::cout << "ok\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "ERROR: " << e.what() << "\n";
        return 1;
    }
}
