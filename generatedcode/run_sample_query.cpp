#include "query_impl.hpp"
#include "read_api.hpp"

#include <iostream>
#include <string>
#include <vector>

int main(int argc, char** argv) {
    const std::string bff_dir = argc > 1 ? argv[1] : "bff_store";

    BffOpenOptions options;
    options.cache_footer = true;
    options.validate_footer_checksum = false;
    options.io_mode = BffIoMode::Buffered;

    Database* db = build_bff_query_database(bff_dir, options);
    std::vector<QueryResult> results = query(db, {
        "1 q1 \"90\"",
        "2 q2 \"1995-03-01\"",
        "3 q3 \"BUILDING\"",
        "4 q4 \"Brand#11\" \"SM BOX\"",
        "5 q5 \"supplier\"",
        "6 q6 \"AIR\" \"RAIL\" \"1995-03-01\"",
        "7 q7 \"goldenrod\" \"COPPER\"",
        "8 q8 \"13\" \"14\" \"15\"",
    });
    destroy_bff_query_database(db);

    for (const auto& result : results) {
        if (!result.error.empty()) {
            std::cerr << result.error << "\n";
            return 1;
        }
        std::cout << "Q" << result.query_id << " " << result.req_id
                  << " elapsed_ms=" << result.elapsed_ms << "\n";
    }

    return 0;
}
