// shm_test.cpp
//
// Build:
//   g++ -std=c++20 -O2 -Wall -Wextra shm_test.cpp -o
//   shm_test
//
// Usage (run in separate processes):
//   ./shm_test init   /my_shm_name
//   ./shm_test read   /my_shm_name
//   ./shm_test deinit /my_shm_name
//
// Design:
// - Uses POSIX shared memory (shm_open) -> NOT persistent across reboot
// - Maps at a fixed virtual address using MAP_FIXED_NOREPLACE (safe default)
// - Stores normal pointer-based C++ objects (vector + string)
// - Custom bump allocator inside shared region
// - Write once, then read forever pattern
//
// IMPORTANT GOTCHAS:
//
// 1) Fixed address requirement
//    Every process MUST successfully map the region at the same address.
//    If the address is already in use, mmap will FAIL (by design).
//    If this happens, change kFixedAddr.
//
// 2) std::vector growth leaks space
//    Bump allocator never frees memory.
//    Any vector reallocation permanently consumes region space.
//    Solution: call reserve(N) during init if size is known.
//
// 3) ABI / toolchain lock-in
//    You MUST use the same compiler, standard library and build flags
//    to read the region. Object layouts must match exactly.
//
// 4) Single-writer rule
//    This example assumes one writer during init and read-only afterwards.
//    Multiple writers require locking and a real allocator.
//
// 5) No persistence guarantee
//    POSIX shm is lost on reboot.
//    Call shm_unlink() when you want to discard it manually.
//
// If mmap fails:
//   Pick another kFixedAddr.
//

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <new>
#include <stdexcept>
#include <string>
#include <vector>

#include <errno.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

// ---------------- Configuration ----------------
static constexpr std::uintptr_t kFixedAddr = 0x3f0000000000ULL;
static constexpr std::size_t kRegionSize = 64 * 1024 * 1024; // 64 MiB

static constexpr uint64_t kMagic = 0x53484D5F4D41505FULL;
static constexpr uint32_t kVersion = 1;

// ---------------- Helpers ----------------
static void throw_errno(const char *what) {
  throw std::runtime_error(std::string(what) + ": " + std::strerror(errno));
}

static std::size_t align_up(std::size_t x, std::size_t a) {
  return (x + (a - 1)) & ~(a - 1);
}

// ---------------- Shared region header + bump allocator ----------------
struct alignas(64) RegionHeader {
  uint64_t magic;
  uint32_t version;
  uint32_t ready;

  std::size_t region_size;
  std::size_t bump_off;
  std::size_t bump_end;
  std::size_t root_off;
};

struct BumpArena {
  RegionHeader *hdr;
  std::byte *base;

  void *allocate(std::size_t n, std::size_t align) {
    std::size_t off = align_up(hdr->bump_off, align);
    if (off + n > hdr->bump_end)
      throw std::bad_alloc();
    hdr->bump_off = off + n;
    return base + off;
  }

  void deallocate(void *, std::size_t) noexcept {}
};

// ---------------- STL allocator ----------------
template <class T> struct ShmAllocator {
  using value_type = T;
  BumpArena *arena = nullptr;

  ShmAllocator() = default;
  explicit ShmAllocator(BumpArena *a) : arena(a) {}

  template <class U>
  ShmAllocator(const ShmAllocator<U> &o) noexcept : arena(o.arena) {}

  T *allocate(std::size_t n) {
    void *p = arena->allocate(sizeof(T) * n, alignof(T));
    return static_cast<T *>(p);
  }

  void deallocate(T *, std::size_t) noexcept {}

  template <class U> bool operator==(const ShmAllocator<U> &o) const noexcept {
    return arena == o.arena;
  }
  template <class U> bool operator!=(const ShmAllocator<U> &o) const noexcept {
    return !(*this == o);
  }
};

// ---------------- Data types ----------------
using ShmCharAlloc = ShmAllocator<char>;
using ShmString = std::basic_string<char, std::char_traits<char>, ShmCharAlloc>;

struct Record {
  uint32_t id;
  int64_t value;
  ShmString name;

  Record(uint32_t i, int64_t v, ShmString &&n)
      : id(i), value(v), name(std::move(n)) {}
};

using ShmRecAlloc = ShmAllocator<Record>;
using ShmVector = std::vector<Record, ShmRecAlloc>;

struct Root {
  ShmVector records;
  explicit Root(const ShmRecAlloc &a) : records(a) {}
};

// ---------------- POSIX shm helpers ----------------
static int shm_open_create(const char *name) {
  int fd = shm_open(name, O_CREAT | O_EXCL | O_RDWR, 0600);
  if (fd == -1)
    throw_errno("shm_open(create)");
  if (ftruncate(fd, kRegionSize) != 0)
    throw_errno("ftruncate");
  return fd;
}

static int shm_open_existing(const char *name) {
  int fd = shm_open(name, O_RDWR, 0600);
  if (fd == -1)
    throw_errno("shm_open(existing)");
  return fd;
}

static void *map_fixed(int fd, bool writable) {
  int prot = PROT_READ | (writable ? PROT_WRITE : 0);
  int flags = MAP_SHARED | MAP_FIXED_NOREPLACE;

  void *addr = mmap((void *)kFixedAddr, kRegionSize, prot, flags, fd, 0);
  if (addr == MAP_FAILED)
    throw_errno("mmap");
  return addr;
}

// ---------------- Logic ----------------
static Root *root_ptr(RegionHeader *h, std::byte *base) {
  return reinterpret_cast<Root *>(base + h->root_off);
}

static void do_init(const char *name) {
  int fd = shm_open_create(name);
  void *addr = map_fixed(fd, true);

  auto *base = (std::byte *)addr;
  auto *hdr = (RegionHeader *)base;

  std::memset(hdr, 0, sizeof(*hdr));
  hdr->magic = kMagic;
  hdr->version = kVersion;
  hdr->ready = 0;
  hdr->region_size = kRegionSize;

  hdr->bump_off = align_up(sizeof(RegionHeader), 64);
  hdr->bump_end = kRegionSize;

  BumpArena arena{hdr, base};

  void *root_mem = arena.allocate(sizeof(Root), alignof(Root));
  hdr->root_off = (std::byte *)root_mem - base;

  ShmRecAlloc rec_alloc(&arena);
  Root *root = new (root_mem) Root(rec_alloc);

  root->records.reserve(3);

  auto make = [&](const char *s) { return ShmString(s, ShmCharAlloc(&arena)); };

  root->records.emplace_back(1, 100, make("alpha"));
  root->records.emplace_back(2, 200, make("beta"));
  root->records.emplace_back(3, 300, make("gamma"));

  msync(addr, kRegionSize, MS_SYNC);
  __atomic_store_n(&hdr->ready, 1u, __ATOMIC_RELEASE);

  std::cout << "Initialized at 0x" << std::hex << kFixedAddr << std::dec
            << "\n";
  close(fd);
}

static void do_read(const char *name) {
  int fd = shm_open_existing(name);
  void *addr = map_fixed(fd, false);

  auto *base = (std::byte *)addr;
  auto *hdr = (RegionHeader *)base;

  if (hdr->magic != kMagic)
    throw std::runtime_error("bad magic");
  if (!__atomic_load_n(&hdr->ready, __ATOMIC_ACQUIRE))
    throw std::runtime_error("not ready");

  Root *root = root_ptr(hdr, base);

  for (auto &r : root->records)
    std::cout << r.id << " " << r.value << " " << r.name << "\n";

  close(fd);
}

// ---------------- NEW: deinit ----------------
static void do_deinit(const char *name) {
  // Try to open; may already be gone
  int fd = shm_open(name, O_RDWR, 0600);
  if (fd != -1) {
    // Best-effort unmap if mapped at fixed address
    munmap((void *)kFixedAddr, kRegionSize);
    close(fd);
  }

  // Remove shm object
  if (shm_unlink(name) == 0)
    std::cout << "Shared memory " << name << " removed\n";
  else if (errno == ENOENT)
    std::cout << "Shared memory " << name << " already gone\n";
  else
    throw_errno("shm_unlink");
}

int main(int argc, char **argv) {
  if (argc != 3) {
    std::cerr << "Usage: " << argv[0] << " init|read|deinit /name\n";
    return 2;
  }

  try {
    std::string cmd = argv[1];

    if (cmd == "init")
      do_init(argv[2]);
    else if (cmd == "read")
      do_read(argv[2]);
    else if (cmd == "deinit")
      do_deinit(argv[2]);
    else
      throw std::runtime_error("mode must be init/read/deinit");

  } catch (const std::exception &e) {
    std::cerr << "Error: " << e.what() << "\n";
    return 1;
  }
}
