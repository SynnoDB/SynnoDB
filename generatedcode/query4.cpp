#include "query4.hpp"

#include "read_api.hpp"
#include "bff_format.hpp"

#include <stdexcept>
#include <string>
#include <vector>

// SQL:
/** select
    p_partkey,
    p_name,
    p_mfgr,
    p_retailprice
from
    part
where
    p_brand = '[BRAND]'
    and p_container = '[CONTAINER]'
order by
    p_retailprice desc; */

std::vector<std::vector<std::string>> run_q4(Database* db, const Q4Args& args) {
    (void)args;

    // The BFF dataset is opened and its footer decoded once in the writer stage
    // (build_bff_query_database) and handed to every query as `db`. The pruning
    // metadata is therefore already resident in memory: read it from db->footer
    // instead of re-opening the dataset or re-parsing the footer here.
    if (!db || !db->dataset) {
        throw std::runtime_error(
            "run_q4: BFF query database not built (writer stage / STORAGE_DIR?)");
    }
    const BffFooter* footer = db->footer;
    (void)footer;

    // TODO: implement query 4:
    //   * open the tables you need with open_bff_table(db->dataset, name),
    //   * prune row groups/pages/columns using db->footer stats,
    //   * read the needed buffers (read_bff_row_group / read_bff_page),
    //   * decode, filter, aggregate, and order the rows.

    std::vector<std::vector<std::string>> rows;
    rows.push_back({"col1", "col2"}); // TODO: replace with the real column names

    return rows;
}
