#include "query${qid}.hpp"

#include "read_api.hpp"

#include <cstdlib>
#include <stdexcept>
#include <string>
#include <vector>

// SQL:
/** ${query_sql} */

// The BFF dataset is addressed by directory, not by an in-memory Database. The
// runner points STORAGE_DIR at the per-scale-factor dataset directory written
// by the writer stage; `db` is always null here.
static std::string bff_storage_dir() {
    const char* env = std::getenv("STORAGE_DIR");
    if (!env || env[0] == '\0') {
        throw std::runtime_error("STORAGE_DIR not set: cannot locate BFF dataset");
    }
    return env;
}

std::vector<std::vector<std::string>> run_q${qid}(Database* /*db*/, const Q${qid}Args& args) {
    (void)args;

    // Open the dataset through the BFF read API. The skeleton reader returns
    // empty metadata until the read/write APIs are implemented, so this query
    // currently produces only a header row.
    BffOpenOptions open_options;
    BffDataset* dataset = open_bff_dataset(bff_storage_dir(), open_options);
    load_bff_footer(dataset, /*refresh_cache=*/false);

    // TODO: implement query ${qid}:
    //   * choose projected + predicate columns from the args,
    //   * prune row groups/pages using the footer stats,
    //   * read the needed buffers (read_bff_row_group / read_bff_page),
    //   * decode, filter, aggregate, and order the rows.

    std::vector<std::vector<std::string>> rows;
    rows.push_back({"col1", "col2"}); // TODO: replace with the real column names

    close_bff_dataset(dataset);
    return rows;
}
