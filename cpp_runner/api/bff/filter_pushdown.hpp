#pragma once

#include "ingest_types.hpp"

#include <cstdint>
#include <vector>

enum class BffFilterNodeKind : std::uint8_t {
    AlwaysTrue = 0,
    AlwaysFalse = 1,
    Predicate = 2,
    And = 3,
    Or = 4,
    Not = 5,
};

enum class BffPredicateOp : std::uint8_t {
    Equal = 0,
    NotEqual = 1,
    LessThan = 2,
    LessEqual = 3,
    GreaterThan = 4,
    GreaterEqual = 5,
    IsNull = 6,
    IsNotNull = 7,
    InList = 8,
};

enum class BffPruneDecision : std::uint8_t {
    Keep = 0,
    Drop = 1,
    Unknown = 2,
};

struct BffLiteral {
    BffPhysicalType type = BffPhysicalType::Int64;
    bool is_null = false;
    std::vector<std::uint8_t> value;
};

struct BffColumnPredicate {
    std::uint32_t column_id = 0;
    BffPredicateOp op = BffPredicateOp::Equal;
    std::vector<BffLiteral> values;
};

struct BffFilterNode {
    BffFilterNodeKind kind = BffFilterNodeKind::AlwaysTrue;
    BffColumnPredicate predicate;
    std::uint32_t first_child = 0;
    std::uint32_t child_count = 0;
};

struct BffFilterExpr {
    std::uint32_t root_node = 0;
    std::vector<BffFilterNode> nodes;
};

struct BffPageRef {
    std::uint32_t row_group_id = 0;
    std::uint32_t column_id = 0;
    std::uint32_t page_id = 0;
};

struct BffScanRequest {
    BffColumnSelection columns;
    BffFilterExpr filter;
    bool enable_row_group_pruning = true;
    bool enable_page_pruning = true;
    bool keep_unknown = true;
};

struct BffScanPlan {
    std::vector<std::uint32_t> row_group_ids;
    std::vector<BffPageRef> pages;
    std::uint64_t estimated_rows = 0;
    std::uint64_t estimated_bytes = 0;
    std::uint64_t pruned_row_groups = 0;
    std::uint64_t pruned_pages = 0;
};

// Build a metadata-only read plan from footer/table stats. This does not
// evaluate row-level predicates; engines still apply the filter after reading.
BffScanPlan plan_bff_scan(BffTable* table, const BffScanRequest& request);
