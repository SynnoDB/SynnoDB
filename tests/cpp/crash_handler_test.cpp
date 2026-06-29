// Driver for tests/test_crash_handler.py: install the crash handler, tag a running query,
// then fault. The test asserts the resulting stderr carries a SynnoDB crash block (signal,
// query context, symbolized stack) instead of a bare "killed by signal 11".
#include "crash_handler.hpp"

// noinline + a distinctive name so the backtrace / addr2line points unambiguously back here.
__attribute__((noinline)) int crashing_query_body() {
    volatile int* p = reinterpret_cast<volatile int*>(0);  // unmapped page 0
    return *p;                                              // -> SIGSEGV
}

int main() {
    synnodb::install_crash_handler();
    synnodb::set_query_context("run #7 Q42(test-context)");
    return crashing_query_body();
}
