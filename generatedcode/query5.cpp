#include "query5.hpp"

#include "read_api.hpp"
#include "bff_format.hpp"

#include <stdexcept>
#include <string>
#include <vector>

// SQL:
/** select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    s_acctbal
from
    supplier
where
    s_comment like '%[WORD1]%'
order by
    s_acctbal desc; */

std::vector<std::vector<std::string>> run_q5(Database* db, const Q5Args& args) {
    (void)args;

    // The BFF dataset is opened and its footer decoded once in the writer stage
    // (build_bff_query_database) and handed to every query as `db`. The pruning
    // metadata is therefore already resident in memory: read it from db->footer
    // instead of re-opening the dataset or re-parsing the footer here.
    if (!db || !db->dataset) {
        throw std::runtime_error(
            "run_q5: BFF query database not built (writer stage / STORAGE_DIR?)");
    }
    const BffFooter* footer = db->footer;
    (void)footer;

    // TODO: implement query 5:
    //   * open the tables you need with open_bff_table(db->dataset, name),
    //   * prune row groups/pages/columns using db->footer stats,
    //   * read the needed buffers (read_bff_row_group / read_bff_page),
    //   * decode, filter, aggregate, and order the rows.

    std::vector<std::vector<std::string>> rows;
    rows.push_back({"col1", "col2"}); // TODO: replace with the real column names

    return rows;
}
