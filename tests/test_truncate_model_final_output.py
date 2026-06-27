# add parent to path
import sys
import unittest
from pathlib import Path

from synnodb.observability.logging.truncate_model_log import truncate_model_final_output

sys.path.append(str(Path(__file__).parent.parent))


class TestTruncateModelFinalOutput(unittest.TestCase):
    def test_truncate_model_final_output(self):
        """Test example."""
        input = """Summary:
- Implemented query execution for Q1–Q3 with dedicated files, CSV writers, and date handling.
- Updated query dispatch to run Q1–Q3 and emit timing output per run.

Tests not run (not requested).

<BEGIN_FILES>
query_impl.cpp
#include "query_impl.hpp"

#include "query_q1.hpp"
#include "query_q2.hpp"
#include "query_q3.hpp"

#include <chrono>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

struct QueryRequest {
    int id = 0;
    std::string line;
};

#include "args_parser.hpp"

namespace {

void write_stub_csv(int run_id) {
    std::ostringstream filename;
    filename << "result" << run_id << ".csv";
    std::ofstream out(filename.str());
    out << "status\n";
    out << "not_implemented\n";
}

}  // namespace

void query(Database* db) {
    std::vector<QueryRequest> requests;
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.empty()) {
            break;
        }
        std::istringstream iss(line);
        int query_id = 0;
        iss >> query_id;
        if (!iss) {
            continue;
        }
        requests.push_back(QueryRequest{query_id, line});
    }

    for (size_t i = 0; i < requests.size(); ++i) {
        const auto& req = requests[i];
        const int run_id = static_cast<int>(i + 1);
        std::ostringstream filename;
        filename << "result" << run_id << ".csv";
        auto exec_start = std::chrono::steady_clock::now();
        switch (req.id) {
            case 1: {
                Q1Args args = parse_q1(req);
                auto rows = run_q1(*db, args);
                auto exec_end = std::chrono::steady_clock::now();
                auto exec_ms =
                    std::chrono::duration_cast<std::chrono::milliseconds>(exec_end -
                                                                          exec_start)
                        .count();
                std::cout << run_id << " | Execution ms: " << exec_ms << "\n";
                write_q1_csv(filename.str(), rows);
                break;
            }
            case 2: {
                Q2Args args = parse_q2(req);
                auto rows = run_q2(*db, args);
                auto exec_end = std::chrono::steady_clock::now();
                auto exec_ms =
                    std::chrono::duration_cast<std::chrono::milliseconds>(exec_end -
                                                                          exec_start)
                        .count();
                std::cout << run_id << " | Execution ms: " << exec_ms << "\n";
                write_q2_csv(filename.str(), rows);
                break;
            }
            case 3: {
                Q3Args args = parse_q3(req);
                auto rows = run_q3(*db, args);
                auto exec_end = std::chrono::steady_clock::now();
                auto exec_ms =
                    std::chrono::duration_cast<std::chrono::milliseconds>(exec_end -
                                                                          exec_start)
                        .count();
                std::cout << run_id << " | Execution ms: " << exec_ms << "\n";
                write_q3_csv(filename.str(), rows);
                break;
            }
            default: {
                auto exec_end = std::chrono::steady_clock::now();
                auto exec_ms =
                    std::chrono::duration_cast<std::chrono::milliseconds>(exec_end -
                                                                          exec_start)
                        .count();
                std::cout << run_id << " | Execution ms: " << exec_ms << "\n";
                write_stub_csv(filename.str());
                break;
            }
        }
    }
}


// Example code for how to use the parse functions together:
//for (const auto& req : requests) {
//    switch (req.id) {
//        case 1: {
//            Q1Args args = parse_q1(req); 
//            run_q1(db, args);
//            break;
//        }
//        case 2: {
//            Q2Args args = parse_q2(req); 
//            run_q2(db, args);
//            break;
//        }
//        ...
//        case 22: {
//            Q22Args args = parse_q22(req); 
//            run_q22(db, args);
//            break;
//        }
//    }
//}
</END_FILES>

<BEGIN_FILES>
query_q1.hpp
#pragma once

#include "builder_impl.hpp"

#include <string>
#include <vector>

struct Q1Args;

struct Q1Row {
    std::string l_returnflag;
    std::string l_linestatus;
    double sum_qty = 0.0;
    double sum_base_price = 0.0;
    double sum_disc_price = 0.0;
    double sum_charge = 0.0;
    double avg_qty = 0.0;
    double avg_price = 0.0;
    double avg_disc = 0.0;
    int64_t count_order = 0;
};

std::vector<Q1Row> run_q1(const Database& db, const Q1Args& args);
void write_q1_csv(const std::string& filename, const std::vector<Q1Row>& rows);
</END_FILES>

<BEGIN_FILES>
query_q1.cpp
#include "query_q1.hpp"

#include "args_parser.hpp"
#include "query_utils.hpp"

#include <algorithm>
#include <fstream>
#include <unordered_map>

namespace {

struct Q1Key {
    std::string returnflag;
    std::string linestatus;

    bool operator==(const Q1Key& other) const {
        return returnflag == other.returnflag && linestatus == other.linestatus;
    }
};

struct Q1KeyHash {
    size_t operator()(const Q1Key& key) const {
        return std::hash<std::string>()(key.returnflag) ^
               (std::hash<std::string>()(key.linestatus) << 1);
    }
};

struct Q1Agg {
    double sum_qty = 0.0;
    double sum_base_price = 0.0;
    double sum_disc_price = 0.0;
    double sum_charge = 0.0;
    double sum_disc = 0.0;
    int64_t count = 0;
};

}  // namespace

std::vector<Q1Row> run_q1(const Database& db, const Q1Args& args) {
    const int32_t delta = std::stoi(args.DELTA);
    const int32_t base_date = parse_date_string("1998-12-01");
    const int32_t cutoff = base_date - delta;

    std::unordered_map<Q1Key, Q1Agg, Q1KeyHash> aggregates;
    const auto& lineitem = db.lineitem;
    const size_t row_count = lineitem.l_orderkey.size();

    for (size_t i = 0; i < row_count; ++i) {
        if (lineitem.l_shipdate[i] > cutoff) {
            continue;
        }
        Q1Key key{lineitem.l_returnflag[i], lineitem.l_linestatus[i]};
        auto& agg = aggregates[key];
        const double quantity = lineitem.l_quantity[i];
        const double extended = lineitem.l_extendedprice[i];
        const double discount = lineitem.l_discount[i];
        const double tax = lineitem.l_tax[i];
        agg.sum_qty += quantity;
        agg.sum_base_price += extended;
        agg.sum_disc_price += extended * (1.0 - discount);
        agg.sum_charge += extended * (1.0 - discount) * (1.0 + tax);
        agg.sum_disc += discount;
        agg.count += 1;
    }

    std::vector<Q1Row> rows;
    rows.reserve(aggregates.size());
    for (const auto& entry : aggregates) {
        const auto& key = entry.first;
        const auto& agg = entry.second;
        Q1Row row;
        row.l_returnflag = key.returnflag;
        row.l_linestatus = key.linestatus;
        row.sum_qty = agg.sum_qty;
        row.sum_base_price = agg.sum_base_price;
        row.sum_disc_price = agg.sum_disc_price;
        row.sum_charge = agg.sum_charge;
        if (agg.count > 0) {
            row.avg_qty = agg.sum_qty / static_cast<double>(agg.count);
            row.avg_price = agg.sum_base_price / static_cast<double>(agg.count);
            row.avg_disc = agg.sum_disc / static_cast<double>(agg.count);
        }
        row.count_order = agg.count;
        rows.push_back(std::move(row));
    }

    std::sort(rows.begin(), rows.end(), [](const Q1Row& a, const Q1Row& b) {
        if (a.l_returnflag != b.l_returnflag) {
            return a.l_returnflag < b.l_returnflag;
        }
        return a.l_linestatus < b.l_linestatus;
    });

    return rows;
}

void write_q1_csv(const std::string& filename, const std::vector<Q1Row>& rows) {
    std::ofstream out(filename);
    write_csv_header(out,
                     {"l_returnflag",
                      "l_linestatus",
                      "sum_qty",
                      "sum_base_price",
                      "sum_disc_price",
                      "sum_charge",
                      "avg_qty",
                      "avg_price",
                      "avg_disc",
                      "count_order"});

    const std::vector<bool> quote_flags = {true,
                                           true,
                                           false,
                                           false,
                                           false,
                                           false,
                                           false,
                                           false,
                                           false,
                                           false};
    for (const auto& row : rows) {
        write_csv_row(out,
                      {row.l_returnflag,
                       row.l_linestatus,
                       format_double(row.sum_qty),
                       format_double(row.sum_base_price),
                       format_double(row.sum_disc_price),
                       format_double(row.sum_charge),
                       format_double(row.avg_qty),
                       format_double(row.avg_price),
                       format_double(row.avg_disc),
                       std::to_string(row.count_order)},
                      quote_flags);
    }
}
</END_FILES>

<BEGIN_FILES>
query_q2.hpp
#pragma once

#include "builder_impl.hpp"

#include <string>
#include <vector>

struct Q2Args;

struct Q2Row {
    double s_acctbal = 0.0;
    std::string s_name;
    std::string n_name;
    int32_t p_partkey = 0;
    std::string p_mfgr;
    std::string s_address;
    std::string s_phone;
    std::string s_comment;
};

std::vector<Q2Row> run_q2(const Database& db, const Q2Args& args);
void write_q2_csv(const std::string& filename, const std::vector<Q2Row>& rows);
</END_FILES>

<BEGIN_FILES>
query_q2.cpp
#include "query_q2.hpp"

#include "args_parser.hpp"
#include "query_utils.hpp"

#include <algorithm>
#include <cmath>
#include <fstream>
#include <unordered_map>
#include <unordered_set>

namespace {

bool ends_with(const std::string& value, const std::string& suffix) {
    if (suffix.size() > value.size()) {
        return false;
    }
    return std::equal(suffix.rbegin(), suffix.rend(), value.rbegin());
}

}  // namespace

std::vector<Q2Row> run_q2(const Database& db, const Q2Args& args) {
    const int32_t size_filter = std::stoi(args.SIZE);
    const std::string& type_suffix = args.TYPE;
    const std::string& region_name = args.REGION;

    int32_t region_key = -1;
    for (size_t i = 0; i < db.region.r_regionkey.size(); ++i) {
        if (db.region.r_name[i] == region_name) {
            region_key = db.region.r_regionkey[i];
            break;
        }
    }
    if (region_key == -1) {
        return {};
    }

    std::unordered_map<int32_t, std::string> nation_name_by_key;
    nation_name_by_key.reserve(db.nation.n_nationkey.size());
    for (size_t i = 0; i < db.nation.n_nationkey.size(); ++i) {
        if (db.nation.n_regionkey[i] == region_key) {
            nation_name_by_key.emplace(db.nation.n_nationkey[i], db.nation.n_name[i]);
        }
    }

    std::unordered_map<int32_t, size_t> supplier_index_by_key;
    supplier_index_by_key.reserve(db.supplier.s_suppkey.size());
    for (size_t i = 0; i < db.supplier.s_suppkey.size(); ++i) {
        if (nation_name_by_key.find(db.supplier.s_nationkey[i]) !=
            nation_name_by_key.end()) {
            supplier_index_by_key.emplace(db.supplier.s_suppkey[i], i);
        }
    }

    std::unordered_map<int32_t, size_t> part_index_by_key;
    part_index_by_key.reserve(db.part.p_partkey.size());
    for (size_t i = 0; i < db.part.p_partkey.size(); ++i) {
        if (db.part.p_size[i] == size_filter &&
            ends_with(db.part.p_type[i], type_suffix)) {
            part_index_by_key.emplace(db.part.p_partkey[i], i);
        }
    }

    std::unordered_map<int32_t, double> min_supply_cost;
    min_supply_cost.reserve(part_index_by_key.size());
    for (size_t i = 0; i < db.partsupp.ps_partkey.size(); ++i) {
        const int32_t partkey = db.partsupp.ps_partkey[i];
        const int32_t suppkey = db.partsupp.ps_suppkey[i];
        if (part_index_by_key.find(partkey) == part_index_by_key.end()) {
            continue;
        }
        if (supplier_index_by_key.find(suppkey) == supplier_index_by_key.end()) {
            continue;
        }
        const double cost = db.partsupp.ps_supplycost[i];
        auto it = min_supply_cost.find(partkey);
        if (it == min_supply_cost.end() || cost < it->second) {
            min_supply_cost[partkey] = cost;
        }
    }

    std::vector<Q2Row> rows;
    for (size_t i = 0; i < db.partsupp.ps_partkey.size(); ++i) {
        const int32_t partkey = db.partsupp.ps_partkey[i];
        const int32_t suppkey = db.partsupp.ps_suppkey[i];
        auto part_it = part_index_by_key.find(partkey);
        if (part_it == part_index_by_key.end()) {
            continue;
        }
        auto supp_it = supplier_index_by_key.find(suppkey);
        if (supp_it == supplier_index_by_key.end()) {
            continue;
        }
        auto min_it = min_supply_cost.find(partkey);
        if (min_it == min_supply_cost.end()) {
            continue;
        }
        const double cost = db.partsupp.ps_supplycost[i];
        if (std::abs(cost - min_it->second) > 1e-9) {
            continue;
        }

        const size_t supplier_index = supp_it->second;
        const size_t part_index = part_it->second;
        Q2Row row;
        row.s_acctbal = db.supplier.s_acctbal[supplier_index];
        row.s_name = db.supplier.s_name[supplier_index];
        row.n_name = nation_name_by_key[db.supplier.s_nationkey[supplier_index]];
        row.p_partkey = partkey;
        row.p_mfgr = db.part.p_mfgr[part_index];
        row.s_address = db.supplier.s_address[supplier_index];
        row.s_phone = db.supplier.s_phone[supplier_index];
        row.s_comment = db.supplier.s_comment[supplier_index];
        rows.push_back(std::move(row));
    }

    std::sort(rows.begin(), rows.end(), [](const Q2Row& a, const Q2Row& b) {
        if (a.s_acctbal != b.s_acctbal) {
            return a.s_acctbal > b.s_acctbal;
        }
        if (a.n_name != b.n_name) {
            return a.n_name < b.n_name;
        }
        if (a.s_name != b.s_name) {
            return a.s_name < b.s_name;
        }
        return a.p_partkey < b.p_partkey;
    });

    return rows;
}

void write_q2_csv(const std::string& filename, const std::vector<Q2Row>& rows) {
    std::ofstream out(filename);
    write_csv_header(out,
                     {"s_acctbal",
                      "s_name",
                      "n_name",
                      "p_partkey",
                      "p_mfgr",
                      "s_address",
                      "s_phone",
                      "s_comment"});

    const std::vector<bool> quote_flags = {false, true, true, false, true, true, true, true};
    for (const auto& row : rows) {
        write_csv_row(out,
                      {format_double(row.s_acctbal),
                       row.s_name,
                       row.n_name,
                       std::to_string(row.p_partkey),
                       row.p_mfgr,
                       row.s_address,
                       row.s_phone,
                       row.s_comment},
                      quote_flags);
    }
}
</END_FILES>

<BEGIN_FILES>
query_q3.hpp
#pragma once

#include "builder_impl.hpp"

#include <string>
#include <vector>

struct Q3Args;

struct Q3Row {
    int32_t l_orderkey = 0;
    double revenue = 0.0;
    std::string o_orderdate;
    int32_t o_shippriority = 0;
};

std::vector<Q3Row> run_q3(const Database& db, const Q3Args& args);
void write_q3_csv(const std::string& filename, const std::vector<Q3Row>& rows);
</END_FILES>

<BEGIN_FILES>
query_q3.cpp
#include "query_q3.hpp"

#include "args_parser.hpp"
#include "query_utils.hpp"

#include <algorithm>
#include <fstream>
#include <unordered_map>
#include <unordered_set>

namespace {

struct OrderInfo {
    int32_t orderdate = 0;
    int32_t shippriority = 0;
};

}  // namespace

std::vector<Q3Row> run_q3(const Database& db, const Q3Args& args) {
    const std::string& segment = args.SEGMENT;
    const int32_t date_cutoff = parse_date_string(args.DATE);

    std::unordered_set<int32_t> customer_keys;
    customer_keys.reserve(db.customer.c_custkey.size());
    for (size_t i = 0; i < db.customer.c_custkey.size(); ++i) {
        if (db.customer.c_mktsegment[i] == segment) {
            customer_keys.insert(db.customer.c_custkey[i]);
        }
    }

    std::unordered_map<int32_t, OrderInfo> orders_by_key;
    orders_by_key.reserve(db.orders.o_orderkey.size());
    for (size_t i = 0; i < db.orders.o_orderkey.size(); ++i) {
        if (db.orders.o_orderdate[i] >= date_cutoff) {
            continue;
        }
        if (customer_keys.find(db.orders.o_custkey[i]) == customer_keys.end()) {
            continue;
        }
        orders_by_key.emplace(db.orders.o_orderkey[i],
                              OrderInfo{db.orders.o_orderdate[i],
                                        db.orders.o_shippriority[i]});
    }

    std::unordered_map<int32_t, double> revenue_by_order;
    revenue_by_order.reserve(orders_by_key.size());
    for (size_t i = 0; i < db.lineitem.l_orderkey.size(); ++i) {
        const int32_t orderkey = db.lineitem.l_orderkey[i];
        auto it = orders_by_key.find(orderkey);
        if (it == orders_by_key.end()) {
            continue;
        }
        if (db.lineitem.l_shipdate[i] <= date_cutoff) {
            continue;
        }
        const double revenue =
            db.lineitem.l_extendedprice[i] * (1.0 - db.lineitem.l_discount[i]);
        revenue_by_order[orderkey] += revenue;
    }

    std::vector<Q3Row> rows;
    rows.reserve(revenue_by_order.size());
    for (const auto& entry : revenue_by_order) {
        const int32_t orderkey = entry.first;
        const auto info_it = orders_by_key.find(orderkey);
        if (info_it == orders_by_key.end()) {
            continue;
        }
        Q3Row row;
        row.l_orderkey = orderkey;
        row.revenue = entry.second;
        row.o_orderdate = format_date_string(info_it->second.orderdate);
        row.o_shippriority = info_it->second.shippriority;
        rows.push_back(std::move(row));
    }

    std::sort(rows.begin(), rows.end(), [](const Q3Row& a, const Q3Row& b) {
        if (a.revenue != b.revenue) {
            return a.revenue > b.revenue;
        }
        if (a.o_orderdate != b.o_orderdate) {
            return a.o_orderdate < b.o_orderdate;
        }
        return a.l_orderkey < b.l_orderkey;
    });

    return rows;
}

void write_q3_csv(const std::string& filename, const std::vector<Q3Row>& rows) {
    std::ofstream out(filename);
    write_csv_header(
        out, {"l_orderkey", "revenue", "o_orderdate", "o_shippriority"});

    const std::vector<bool> quote_flags = {false, false, true, false};
    for (const auto& row : rows) {
        write_csv_row(out,
                      {std::to_string(row.l_orderkey),
                       format_double(row.revenue),
                       row.o_orderdate,
                       std::to_string(row.o_shippriority)},
                      quote_flags);
    }
}
</END_FILES>"""
        out = truncate_model_final_output(input)

        # count out lines
        out_lines = out.splitlines()
        print(f"input lines: {len(input.splitlines())}, output lines: {len(out_lines)}")
        self.assertEqual(len(out_lines), 165)


if __name__ == "__main__":
    unittest.main()
