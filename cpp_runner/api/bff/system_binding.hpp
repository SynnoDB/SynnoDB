#pragma once

#include "filter_pushdown.hpp"
#include "ingest_types.hpp"

#include <cstdint>
#include <string>
#include <vector>

enum class BffEngineKind : std::uint8_t {
    DuckDB = 0,
    DataFusion = 1,
};

enum class BffFilterSupport : std::uint8_t {
    Unsupported = 0,
    Inexact = 1,
    Exact = 2,
};

struct BffBindingColumn {
    std::uint32_t column_id = 0;
    std::string name;
    BffPhysicalType physical_type = BffPhysicalType::Int64;
    bool nullable = false;
};

struct BffBindingSchema {
    std::vector<BffBindingColumn> columns;
};

struct BffBindingRequest {
    BffEngineKind engine = BffEngineKind::DuckDB;
    std::string dataset_path;
    std::string table_name;
    BffColumnSelection projected_columns;
    BffColumnSelection filter_columns;
    BffFilterExpr filter;
    std::uint64_t limit = 0;
    BffOpenOptions open_options;
    BffReadOptions read_options;
};

struct BffBindingPlan {
    BffBindingSchema schema;
    BffScanPlan scan_plan;
    std::vector<BffFilterSupport> filter_support;
    bool limit_pushdown_safe = false;
};

struct BffBindingColumnBuffer {
    std::uint32_t column_id = 0;
    BffPhysicalType physical_type = BffPhysicalType::Int64;
    BffBuffer* values = nullptr;
    BffBuffer* validity = nullptr;
    BffBuffer* offsets = nullptr;
};

struct BffBindingBatch {
    std::uint64_t row_count = 0;
    std::vector<BffBindingColumnBuffer> columns;
};

// Adapter-facing planning hook. DuckDB table functions and DataFusion
// TableProvider implementations translate their native projection/filter/limit
// inputs into BffBindingRequest, then call this to get a BFF scan plan.
BffBindingPlan plan_bff_binding_scan(BffTable* table, const BffBindingRequest& request);

// Adapter-facing batch read hook. Implementations may return zero-copy-friendly
// buffers when the BFF page layout matches the target engine representation.
BffBindingBatch read_bff_binding_batch(
    BffTable* table,
    const BffBindingPlan& plan,
    std::uint64_t batch_index);

void release_bff_binding_batch(BffBindingBatch* batch);

struct BffSystemBindingApi {
    BffBindingPlan (*plan_scan)(BffTable*, const BffBindingRequest&);
    BffBindingBatch (*read_batch)(BffTable*, const BffBindingPlan&, std::uint64_t);
    void (*release_batch)(BffBindingBatch*);
};
