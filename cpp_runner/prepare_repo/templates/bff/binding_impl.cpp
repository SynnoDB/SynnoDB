#include "system_binding.hpp"
#include "bff_format.hpp"

// FILE_VERSION: 1
//
// Skeleton implementation of the system-binding adapter hooks (DuckDB table
// functions / DataFusion TableProvider). These are NOT exercised by the C++
// runner's CSV query path; they exist so libwriter.so's IngestApi is fully
// defined and so future engine bindings have an entry point to implement.

BffBindingPlan plan_bff_binding_scan(
    BffTable* /*table*/,
    const BffBindingRequest& /*request*/) {
    return {};
}

BffBindingBatch read_bff_binding_batch(
    BffTable* /*table*/,
    const BffBindingPlan& /*plan*/,
    std::uint64_t /*batch_index*/) {
    return {};
}

void release_bff_binding_batch(BffBindingBatch* /*batch*/) {
}
