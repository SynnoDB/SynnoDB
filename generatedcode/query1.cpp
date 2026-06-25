#include "query1.hpp"

#include "read_api.hpp"
#include "bff_format.hpp"
#include "query_utils.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <string>
#include <vector>
#include <map>

// SQL:
/** select
    l_returnflag,
    l_linestatus,
    sum(l_quantity) as sum_qty,
    sum(l_extendedprice) as sum_base_price,
    sum(l_extendedprice*(1-l_discount)) as sum_disc_price,
    sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge,
    avg(l_quantity) as avg_qty,
    avg(l_extendedprice) as avg_price,
    avg(l_discount) as avg_disc,
    count(*) as count_order
from
    lineitem
where
    l_shipdate <= date '1998-12-01' - interval '[DELTA]' day
group by
    l_returnflag,
    l_linestatus
order by
    l_returnflag,
    l_linestatus; */

std::vector<std::vector<std::string>> run_q1(Database* db, const Q1Args& args) {
    if (!db || !db->dataset) {
        throw std::runtime_error(
            "run_q1: BFF query database not built (writer stage / STORAGE_DIR?)");
    }

    // ----------------------------------------------------------------
    // 1. Compute the cutoff date in CSF epoch days
    //    l_shipdate <= '1998-12-01' - DELTA days
    // ----------------------------------------------------------------
    int delta = std::stoi(args.DELTA);
    // Parse base date to unix days then subtract delta
    int32_t base_unix = parse_date_to_unix_days("1998-12-01");
    int32_t cutoff_unix = base_unix - delta;
    int32_t cutoff_csf = cutoff_unix - CSF_DATE_EPOCH_OFFSET;

    // ----------------------------------------------------------------
    // 2. Open the lineitem table
    // ----------------------------------------------------------------
    BffTable* tbl = open_bff_table(db->dataset, "lineitem");
    if (!tbl) throw std::runtime_error("run_q1: cannot open lineitem table");

    const CsfTableFooter& ft = *tbl->csf_footer;

    // Column IDs (matching write_impl.cpp lineitem spec, 0-based):
    // 0=l_orderkey, 1=l_partkey, 2=l_suppkey, 3=l_linenumber,
    // 4=l_quantity, 5=l_extendedprice, 6=l_discount, 7=l_tax,
    // 8=l_returnflag, 9=l_linestatus, 10=l_shipdate, ...
    uint32_t col_shipdate      = find_col_id(ft, "l_shipdate");
    uint32_t col_returnflag    = find_col_id(ft, "l_returnflag");
    uint32_t col_linestatus    = find_col_id(ft, "l_linestatus");
    uint32_t col_quantity      = find_col_id(ft, "l_quantity");
    uint32_t col_extprice      = find_col_id(ft, "l_extendedprice");
    uint32_t col_discount      = find_col_id(ft, "l_discount");
    uint32_t col_tax           = find_col_id(ft, "l_tax");

    // Dict references for returnflag and linestatus
    const CsfColMeta& meta_shipdate   = ft.cols[col_shipdate];
    const CsfColMeta& meta_returnflag = ft.cols[col_returnflag];
    const CsfColMeta& meta_linestatus = ft.cols[col_linestatus];
    const CsfColMeta& meta_quantity   = ft.cols[col_quantity];
    const CsfColMeta& meta_extprice   = ft.cols[col_extprice];
    const CsfColMeta& meta_discount   = ft.cols[col_discount];
    const CsfColMeta& meta_tax        = ft.cols[col_tax];

    // ----------------------------------------------------------------
    // 3. Build scan plan: prune segments where shipdate_min > cutoff_csf
    //    (since lineitem is sorted by l_shipdate ASC, we can prune
    //     segments whose minimum shipdate is already > cutoff)
    // ----------------------------------------------------------------
    BffScanRequest req;
    req.enable_row_group_pruning = true;
    req.enable_page_pruning = false;
    req.keep_unknown = true;

    // Predicate: l_shipdate <= cutoff_csf
    BffFilterNode pred_node;
    pred_node.kind = BffFilterNodeKind::Predicate;
    pred_node.predicate.column_id = col_shipdate;
    pred_node.predicate.op = BffPredicateOp::LessEqual;
    BffLiteral lit;
    lit.type = BffPhysicalType::Int64;
    lit.is_null = false;
    lit.value.resize(8);
    int64_t cutoff_val = int64_t(cutoff_csf);
    memcpy(lit.value.data(), &cutoff_val, 8);
    pred_node.predicate.values.push_back(lit);

    req.filter.nodes.push_back(pred_node);
    req.filter.root_node = 0;

    BffScanPlan plan = plan_bff_scan(tbl, req);

    // ----------------------------------------------------------------
    // 4. Aggregate per (returnflag_code, linestatus_code) group
    // ----------------------------------------------------------------
    struct Agg {
        int64_t sum_qty        = 0;  // raw * 100
        int64_t sum_extprice   = 0;  // raw * 100
        // disc_price = extprice * (1 - discount), kept as int64 * 100^2
        // charge     = extprice * (1 - discount) * (1 + tax), * 100^3
        // To avoid overflow we accumulate in double for the products
        double  sum_disc_price = 0.0;
        double  sum_charge     = 0.0;
        int64_t sum_discount   = 0;  // raw * 100
        int64_t count          = 0;
    };

    // Key: (returnflag_code << 8) | linestatus_code
    std::map<uint16_t, Agg> groups;

    for (uint32_t seg : plan.row_group_ids) {
        uint32_t nrows = seg_row_count(ft, seg);

        // Read and decode needed columns
        BffBuffer* buf_ship = read_col_block(tbl, seg, col_shipdate);
        BffBuffer* buf_rf   = read_col_block(tbl, seg, col_returnflag);
        BffBuffer* buf_ls   = read_col_block(tbl, seg, col_linestatus);
        BffBuffer* buf_qty  = read_col_block(tbl, seg, col_quantity);
        BffBuffer* buf_ep   = read_col_block(tbl, seg, col_extprice);
        BffBuffer* buf_disc = read_col_block(tbl, seg, col_discount);
        BffBuffer* buf_tax  = read_col_block(tbl, seg, col_tax);

        std::vector<int64_t> v_ship, v_rf, v_ls, v_qty, v_ep, v_disc, v_tax;

        bool ok = true;
        ok &= decode_int_block(buf_ship, meta_shipdate,   v_ship);
        ok &= decode_int_block(buf_rf,   meta_returnflag, v_rf);
        ok &= decode_int_block(buf_ls,   meta_linestatus, v_ls);
        ok &= decode_int_block(buf_qty,  meta_quantity,   v_qty);
        ok &= decode_int_block(buf_ep,   meta_extprice,   v_ep);
        ok &= decode_int_block(buf_disc, meta_discount,   v_disc);
        ok &= decode_int_block(buf_tax,  meta_tax,        v_tax);

        release_bff_buffer(buf_ship);
        release_bff_buffer(buf_rf);
        release_bff_buffer(buf_ls);
        release_bff_buffer(buf_qty);
        release_bff_buffer(buf_ep);
        release_bff_buffer(buf_disc);
        release_bff_buffer(buf_tax);

        if (!ok) continue;

        for (uint32_t r = 0; r < nrows; r++) {
            // Filter: l_shipdate <= cutoff_csf
            if (v_ship[r] > cutoff_csf) continue;

            uint8_t rf_code = uint8_t(v_rf[r]);
            uint8_t ls_code = uint8_t(v_ls[r]);
            uint16_t key = uint16_t(rf_code) << 8 | ls_code;

            // Values (stored scaled by 100)
            int64_t qty  = v_qty[r];   // raw = logical * 100
            int64_t ep   = v_ep[r];    // raw = logical * 100
            int64_t disc = v_disc[r];  // raw = logical * 100
            int64_t tax  = v_tax[r];   // raw = logical * 100

            // disc_price = ep * (1 - disc/100) = (ep * (100 - disc)) / 100
            // charge     = disc_price * (1 + tax/100) = (disc_price * (100 + tax)) / 100
            // Use double to avoid overflow on large segments
            double ep_d   = double(ep)   / 100.0;
            double disc_d = double(disc) / 100.0;
            double tax_d  = double(tax)  / 100.0;
            double disc_price = ep_d * (1.0 - disc_d);
            double charge     = disc_price * (1.0 + tax_d);

            Agg& g = groups[key];
            g.sum_qty       += qty;
            g.sum_extprice  += ep;
            g.sum_disc_price += disc_price;
            g.sum_charge     += charge;
            g.sum_discount  += disc;
            g.count++;
        }
    }

    close_bff_table(tbl);

    // ----------------------------------------------------------------
    // 5. Resolve dict codes to strings and sort output
    // ----------------------------------------------------------------
    // returnflag dict_id = meta_returnflag.dict_id
    // linestatus dict_id = meta_linestatus.dict_id
    const std::vector<std::string>& rf_dict =
        ft.dicts[meta_returnflag.dict_id].entries;
    const std::vector<std::string>& ls_dict =
        ft.dicts[meta_linestatus.dict_id].entries;

    // Collect rows and sort by (returnflag string ASC, linestatus string ASC)
    struct OutRow {
        std::string returnflag;
        std::string linestatus;
        int64_t     sum_qty;
        int64_t     sum_extprice;
        double      sum_disc_price;
        double      sum_charge;
        double      avg_qty;
        double      avg_price;
        double      avg_disc;
        int64_t     count;
    };

    std::vector<OutRow> out_rows;
    out_rows.reserve(groups.size());

    for (auto& [key, g] : groups) {
        uint8_t rf_code = uint8_t(key >> 8);
        uint8_t ls_code = uint8_t(key & 0xFF);
        OutRow row;
        row.returnflag    = (rf_code < rf_dict.size()) ? rf_dict[rf_code] : std::to_string(rf_code);
        row.linestatus    = (ls_code < ls_dict.size()) ? ls_dict[ls_code] : std::to_string(ls_code);
        row.sum_qty       = g.sum_qty;
        row.sum_extprice  = g.sum_extprice;
        row.sum_disc_price = g.sum_disc_price;
        row.sum_charge    = g.sum_charge;
        row.avg_qty       = double(g.sum_qty)     / double(g.count) / 100.0;
        row.avg_price     = double(g.sum_extprice) / double(g.count) / 100.0;
        row.avg_disc      = double(g.sum_discount) / double(g.count) / 100.0;
        row.count         = g.count;
        out_rows.push_back(std::move(row));
    }

    std::sort(out_rows.begin(), out_rows.end(), [](const OutRow& a, const OutRow& b) {
        if (a.returnflag != b.returnflag) return a.returnflag < b.returnflag;
        return a.linestatus < b.linestatus;
    });

    // ----------------------------------------------------------------
    // 6. Format and return results
    // ----------------------------------------------------------------
    std::vector<std::vector<std::string>> result;
    result.push_back({
        "l_returnflag", "l_linestatus",
        "sum_qty", "sum_base_price", "sum_disc_price", "sum_charge",
        "avg_qty", "avg_price", "avg_disc", "count_order"
    });

    for (const OutRow& row : out_rows) {
        // sum_qty and sum_base_price are stored *100 so divide
        // sum_disc_price and sum_charge are already in logical units (double)
        char buf[64];

        auto fmt2 = [](double v) -> std::string {
            char b[32];
            snprintf(b, sizeof(b), "%.2f", v);
            return b;
        };

        result.push_back({
            row.returnflag,
            row.linestatus,
            fmt2(double(row.sum_qty)      / 100.0),
            fmt2(double(row.sum_extprice) / 100.0),
            fmt2(row.sum_disc_price),
            fmt2(row.sum_charge),
            fmt2(row.avg_qty),
            fmt2(row.avg_price),
            fmt2(row.avg_disc),
            std::to_string(row.count)
        });
        (void)buf;
    }

    return result;
}
