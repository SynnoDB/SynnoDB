#pragma once

#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <unistd.h>

class PerfControl {
public:
  static void enable() { instance().write("enable\n"); }
  static void disable() { instance().write("disable\n"); }

private:
  int fd_;

  PerfControl() : fd_(read_fd_from_env()) {}

  static PerfControl &instance() {
    static PerfControl inst;
    return inst;
  }

  static int read_fd_from_env() {
    const char *s = std::getenv("PERF_CTL_FD");
    if (!s || !*s)
      return -1;
    return std::atoi(s);
  }

  void write(const char *cmd) {
    if (fd_ < 0)
      return; // not under perf
    size_t n = std::strlen(cmd);
    const char *p = cmd;

    while (n) {
      ssize_t w = ::write(fd_, p, n);
      if (w > 0) {
        p += static_cast<size_t>(w);
        n -= static_cast<size_t>(w);
        continue;
      }
      if (w == -1 && errno == EINTR)
        continue;

      throw std::runtime_error("PerfControl: write failed: " +
                               std::string(std::strerror(errno)));
    }
  }

  PerfControl(const PerfControl &) = delete;
  PerfControl &operator=(const PerfControl &) = delete;
};
