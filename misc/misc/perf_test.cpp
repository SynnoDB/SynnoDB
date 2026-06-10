#include "perf_control.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <numeric>
#include <random>
#include <string>
#include <unordered_map>
#include <vector>

static inline uint64_t scramble(uint64_t x) {
  x ^= (x << 13);
  x ^= (x >> 7);
  x ^= (x << 17);
  return x;
}

static void init_vector(std::vector<uint64_t> &v, uint64_t &acc) {
  for (size_t i = 0; i < v.size(); ++i) {
    acc = scramble(acc);
    v[i] = acc + i;
  }
}

static void branchy_mix(std::vector<uint64_t> &v, uint64_t &acc) {
  for (size_t i = 1; i < v.size(); ++i) {
    uint64_t x = v[i];
    if (x & 1)
      acc += (x * 0x9e3779b97f4a7c15ULL);
    else
      acc ^= (x >> 3);

    v[i - 1] ^= acc;
  }
}

static void rotate_mix(std::vector<uint64_t> &v, uint64_t &acc) {
  std::rotate(v.begin(), v.begin() + (acc % v.size()), v.end());
}

static uint64_t work(size_t n) {
  std::vector<uint64_t> v(n);
  uint64_t acc = 0x12345678abcdefULL;

  init_vector(v, acc);

  for (size_t r = 0; r < 40; ++r) {
    branchy_mix(v, acc);
    rotate_mix(v, acc);
  }

  return acc;
}

struct Node {
  uint64_t key;
  double weight;
  std::array<uint64_t, 4> pad;
};

static uint64_t sort_nodes(size_t n) {
  std::vector<Node> nodes(n);
  uint64_t acc = 0x9e3779b97f4a7c15ULL;
  for (size_t i = 0; i < n; ++i) {
    acc = scramble(acc);
    nodes[i] = Node{acc, static_cast<double>(acc % 10000) / 100.0, {acc, i, acc ^ i, acc + i}};
  }
  std::sort(nodes.begin(), nodes.end(),
            [](const Node &a, const Node &b) { return a.weight < b.weight; });
  return std::accumulate(nodes.begin(), nodes.end(), uint64_t{0},
                         [](uint64_t s, const Node &n) { return s ^ n.key; });
}

static uint64_t map_work(size_t n) {
  std::unordered_map<uint64_t, uint64_t> m;
  m.reserve(n * 2);
  uint64_t acc = 0xdeadbeefcafebabeULL;
  for (size_t i = 0; i < n; ++i) {
    acc = scramble(acc + i);
    m.emplace(acc, i);
  }
  for (size_t i = 0; i < n; ++i) {
    auto it = m.find(acc ^ i);
    if (it != m.end())
      acc ^= it->second;
    else
      acc = scramble(acc);
  }
  return acc;
}

static uint64_t string_work(size_t n) {
  std::vector<std::string> words;
  words.reserve(n);
  for (size_t i = 0; i < n; ++i) {
    words.push_back("word_" + std::to_string(i) + "_xxxxxxxx");
  }
  std::sort(words.begin(), words.end());
  size_t total = 0;
  for (const auto &w : words)
    total += w.size();
  return static_cast<uint64_t>(total);
}

int main() {
  constexpr size_t N = 1 << 23;

  // warm-up / ingest (not profiled)
  volatile uint64_t warm = work(N / 8);
  (void)warm;

  // start perf recording
  PerfControl::enable();

  auto t0 = std::chrono::steady_clock::now();
  volatile uint64_t result = work(N);
  volatile uint64_t result2 = sort_nodes(N / 32);
  volatile uint64_t result3 = map_work(N / 64);
  volatile uint64_t result4 = string_work(N / 128);
  auto t1 = std::chrono::steady_clock::now();

  // stop perf recording
  PerfControl::disable();

  std::chrono::duration<double> dt = t1 - t0;
  std::cout << "result=" << result << " time=" << dt.count() << "s\n";
  std::cout << "extra=" << result2 << "," << result3 << "," << result4 << "\n";

  std::cout << "running unprofiled\n";
  volatile uint64_t result5 = work(N);
  std::cout << "result=" << result5 << "\n";

  return 0;
}
