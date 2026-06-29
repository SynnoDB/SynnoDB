#include "query${qid}.hpp"
#include "column_egress.hpp"

#include <algorithm>
#include <array>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

// SQL:
/** ${query_sql} */

std::shared_ptr<arrow::Table> run_q${qid}(Database* db, const Q${qid}Args& args) {
    if (!db) {
        throw std::runtime_error("run_q${qid}: db is null");
    }
    using namespace synnodb::egress;

    // TODO: implement query logic here, accumulating each output column into a typed C++ vector.
    // CRITICAL for exactness: a DECIMAL output column (SUM of decimals, a decimal expression)
    // must be accumulated as the exact unscaled integer in __int128 - never through double -
    // and emitted with decimal_column(values, precision, scale). The scale is the column's
    // DuckDB scale (e.g. SUM(l_extendedprice) -> DECIMAL(38,2), scale 2; a product of two
    // DECIMAL(_,2) -> scale 4). A HUGEINT output is the same exact-integer family:
    // hugeint_column(values) or decimal_column(values, 38, 0). Only genuinely DOUBLE columns
    // (AVG, ...) use double_column.
    //
    // Pick the builder by the output column's value FAMILY; column_egress emits the exact
    // DuckDB/Arrow type for you (it casts the canonical build to `target`, failing loudly if a
    // value does not fit). One builder per family - there is no type you must special-case:
    //   decimal_column(v, precision, scale)  DECIMAL - exact, from __int128 (precision > 38 -> decimal256)
    //   hugeint_column(v)                    HUGEINT - exact, from __int128
    //   integer_column(v)                    signed/unsigned integers from any C++ integer width
    //   uint64_column(v)                     UBIGINT / uint64_t values above INT64_MAX
    //   int64_column(v)                      BIGINT compatibility alias; prefer integer_column for new code
    //   double_column(v)                     DOUBLE;  double_column(v, {}, arrow::float32()) for REAL
    //   string_column(v)                     VARCHAR
    //   bool_column(v)                       BOOLEAN
    //   date_column(v)                       DATE (int32 days since 1970-01-01)
    //   timestamp_column(v)                  TIMESTAMP (int64 microseconds since 1970-01-01)
    // If a supported flat scalar output family is missing, extend column_egress.hpp with a
    // reusable exact builder and call it here; do not build ad hoc Arrow arrays or lossy strings
    // in the query implementation.
    //
    // NULLs: pass a Validity mask (valid[i]==0 -> NULL at row i) as the next argument when a
    // result column can be NULL (LEFT JOIN miss, MIN/MAX/AVG over an empty/all-null group,
    // NULLIF, a NULL literal). Never substitute 0/""/the epoch for a NULL.

    // TODO: replace with the real output columns, in DuckDB's column order, e.g.:
    //   std::vector<std::string>  l_returnflag;
    //   std::vector<__int128>     sum_qty;       // exact, scaled by 10^2
    //   std::vector<double>       avg_qty;       // genuinely DOUBLE in DuckDB
    //   std::vector<int32_t>      count_order;   // narrow C++ vector is fine if the value range proves it
    //   std::vector<std::string>  comment;       // may be NULL
    //   egress::Validity          comment_valid; // 1 = present, 0 = NULL (same length as comment)
    //   ... fill them from db ...
    //   return make_table({
    //       {"l_returnflag", string_column(l_returnflag)},
    //       {"sum_qty",      decimal_column(sum_qty, 38, 2)},
    //       {"avg_qty",      double_column(avg_qty)},
    //       {"count_order",  integer_column(count_order, {}, arrow::int64())},
    //       {"o_comment",    string_column(comment, comment_valid)},
    //   });

    return make_table({});  // TODO: build and return the real result
}
