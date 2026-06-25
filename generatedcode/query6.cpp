#include "query6.hpp"

#include "read_api.hpp"
#include "bff_format.hpp"

#include <stdexcept>
#include <string>
#include <vector>

// SQL:
/** select
    l_shipmode,
    count(*) as total_shipments,
    sum(l_quantity) as total_qty
from
    lineitem
where
    l_shipmode in ('[SHIPMODE1]', '[SHIPMODE2]')
    and l_shipdate >= date '[DATE]'
group by
    l_shipmode
order by
    l_shipmode; */

std::vector<std::vector<std::string>> run_q6(Database* db, const Q6Args& args) {
    (void)args;

    // The BFF dataset is opened and its footer decoded once in the writer stage
    // (build_bff_query_database) and handed to every query as `db`. The pruning
    // metadata is therefore already resident in memory: read it from db->footer
    // instead of re-opening the dataset or re-parsing the footer here.
    if (!db || !db->dataset) {
        throw std::runtime_error(
            "run_q6: BFF query database not built (writer stage / STORAGE_DIR?)");
    }
    const BffFooter* footer = db->footer;
    (void)footer;

    // TODO: implement query 6:
    //   * open the tables you need with open_bff_table(db->dataset, name),
    //   * prune row groups/pages/columns using db->footer stats,
    //   * read the needed buffers (read_bff_row_group / read_bff_page),
    //   * decode, filter, aggregate, and order the rows.

    std::vector<std::vector<std::string>> rows;
    rows.push_back({"col1", "col2"}); // TODO: replace with the real column names

    return rows;
}
