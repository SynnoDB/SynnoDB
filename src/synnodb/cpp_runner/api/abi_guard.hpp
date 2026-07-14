#pragma once

#include <exception>
#include <string>

// FILE_VERSION: 1

// Shared plumbing for the C++ plugins' C-ABI shims (loader_api.cpp,
// builder_api.cpp).
//
// A C++ exception must never unwind across the plugin boundary (see
// plugin_abi.h). guarded_call() runs a producer that may throw, converts a
// throw into "return NULL and record the message", and last_error() hands the
// message back to the host -- which rethrows it on its own side, so
// pipeline.hpp's existing stage-failure path still sees a real exception with
// the original text.

namespace synnodb::abi {

// Plugin-owned storage for the most recent failure. Thread-local so a
// multi-threaded loader cannot interleave two failures into one message.
inline thread_local std::string g_last_error;

inline const char* last_error() { return g_last_error.c_str(); }

// Run producer(); on success return its pointer, on throw return nullptr with
// the message recorded in g_last_error.
template <class Producer>
auto guarded_call(const char* what, Producer&& producer) -> decltype(producer()) {
    g_last_error.clear();
    try {
        return producer();
    } catch (const std::exception& e) {
        g_last_error = std::string(what) + " threw std::exception: " + e.what();
    } catch (...) {
        g_last_error = std::string(what) + " threw unknown exception";
    }
    return nullptr;
}

}  // namespace synnodb::abi
