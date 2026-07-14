#include "query_api.hpp"

#include "../hotpatch/plugin_base.hpp"

#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <string>
#include <vector>

// FILE_VERSION: 2

// C-ABI shim for the query plugin. The generated query_impl.cpp implements the
// C++ query(); everything below exists to carry its results across the .so
// boundary as plain C, and to make sure no exception unwinds across it.

namespace {

// Heap-copy a std::string as a NUL-terminated C string. Returns nullptr only on
// allocation failure, which callers below escalate to a batch-level failure.
char* dup_str(const std::string& s) {
    char* out = static_cast<char*>(std::malloc(s.size() + 1));
    if (!out)
        return nullptr;
    std::memcpy(out, s.c_str(), s.size() + 1);
    return out;
}

// The ABI exposes the strings as const char*, but they were malloc'd here, so
// casting the const away to free them is the intended round trip.
void free_cstr(const char* s) { std::free(const_cast<char*>(s)); }

void abi_free_results(SynnoQueryBatchResult* out) {
    if (!out)
        return;
    for (std::size_t i = 0; i < out->len; ++i) {
        free_cstr(out->results[i].query_id);
        free_cstr(out->results[i].req_id);
        free_cstr(out->results[i].trace);
        free_cstr(out->results[i].error);
    }
    std::free(out->results);
    free_cstr(out->stage_error);
    *out = SynnoQueryBatchResult{};
}

// Report a batch-level failure: drop any partial results and carry only the
// error. Falls back to a static literal if even the message cannot be allocated,
// so a failure is never silently downgraded to success.
void set_stage_error(SynnoQueryBatchResult* out, const std::string& msg) {
    static const char kOom[] = "query stage failed; error message could not be allocated";
    abi_free_results(out);
    char* copied = dup_str(msg);
    out->stage_error = copied ? copied : kOom;
}

void abi_query(void*                  database,
               const char* const*     lines,
               std::size_t            n_lines,
               SynnoQueryBatchResult* out) {
    if (!out)
        return;
    *out = SynnoQueryBatchResult{};

    std::vector<QueryResult> results;
    try {
        std::vector<std::string> query_lines;
        query_lines.reserve(n_lines);
        for (std::size_t i = 0; i < n_lines; ++i)
            query_lines.emplace_back(lines[i] ? lines[i] : "");

        results = query(static_cast<Database*>(database), query_lines);
    } catch (const std::exception& e) {
        set_stage_error(out, std::string("query stage threw std::exception: ") + e.what());
        return;
    } catch (...) {
        set_stage_error(out, "query stage threw unknown exception");
        return;
    }

    // stage_error is "" (not null) on the success path: the ABI promises every
    // string is non-null, so the host can read it without a guard.
    out->stage_error = dup_str("");
    if (!out->stage_error) {
        set_stage_error(out, "query stage: out of memory");
        return;
    }
    if (results.empty())
        return;

    auto* arr = static_cast<SynnoQueryResult*>(
        std::calloc(results.size(), sizeof(SynnoQueryResult)));
    if (!arr) {
        set_stage_error(out, "query stage: out of memory allocating results");
        return;
    }
    out->results = arr;
    out->len = results.size();

    for (std::size_t i = 0; i < results.size(); ++i) {
        arr[i].query_id   = dup_str(results[i].query_id);
        arr[i].req_id     = dup_str(results[i].req_id);
        arr[i].trace      = dup_str(results[i].trace);
        arr[i].error      = dup_str(results[i].error);
        arr[i].elapsed_ms = results[i].elapsed_ms;

        if (!arr[i].query_id || !arr[i].req_id || !arr[i].trace || !arr[i].error) {
            set_stage_error(out, "query stage: out of memory copying results");
            return;
        }
    }
}

}  // namespace

static const SynnoQueryApi QUERY = {
    .query = &abi_query,
    .free_results = &abi_free_results,
};

extern "C" __attribute__((visibility("default")))
const void*
plugin_query() {
    return &QUERY;
}
