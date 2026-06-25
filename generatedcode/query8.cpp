#include "query8.hpp"

#include "read_api.hpp"
#include "bff_format.hpp"

#include <stdexcept>
#include <string>
#include <vector>

// SQL:
/** select
    c_custkey,
    c_name,
    c_acctbal
from
    customer
where
    c_acctbal > 0.00
    and substring(c_phone from 1 for 2) in ('[I1]','[I2]','[I3]')
order by
    c_acctbal desc; */

std::vector<std::vector<std::string>> run_q8(Database* db, const Q8Args& args) {
    (void)args;

    // The BFF dataset is opened and its footer decoded once in the writer stage
    // (build_bff_query_database) and handed to every query as `db`. The pruning
    // metadata is therefore already resident in memory: read it from db->footer
    // instead of re-opening the dataset or re-parsing the footer here.
    if (!db || !db->dataset) {
        throw std::runtime_error(
            "run_q8: BFF query database not built (writer stage / STORAGE_DIR?)");
    }
    const BffFooter* footer = db->footer;
    (void)footer;

    // TODO: implement query 8:
    //   * open the tables you need with open_bff_table(db->dataset, name),
    //   * prune row groups/pages/columns using db->footer stats,
    //   * read the needed buffers (read_bff_row_group / read_bff_page),
    //   * decode, filter, aggregate, and order the rows.

    std::vector<std::vector<std::string>> rows;
    rows.push_back({"col1", "col2"}); // TODO: replace with the real column names

    return rows;
}
