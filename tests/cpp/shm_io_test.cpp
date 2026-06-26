// C++ driver exercising the engine-side shm Arrow data plane (read + write).
// Built and run by tests/test_cpp_shm.py against the tested Python transport.
//   shm_io_test read  <path>        -> "rows=N cols=M col0=NAME sum0=S"
//   shm_io_test write <path> <n>    -> creates {a:int64 0..n-1, label:utf8} and writes it
#include "shm_arrow_loader.hpp"
#include "shm_arrow_writer.hpp"

#include <arrow/api.h>
#include <iostream>
#include <memory>
#include <string>

static std::shared_ptr<arrow::Table> make_table(int64_t n) {
    arrow::Int64Builder a;
    arrow::StringBuilder label;
    arrow::Status st;
    for (int64_t i = 0; i < n; ++i) {
        st = a.Append(i);
        if (!st.ok()) throw std::runtime_error(st.ToString());
        st = label.Append("r" + std::to_string(i % 7));
        if (!st.ok()) throw std::runtime_error(st.ToString());
    }
    std::shared_ptr<arrow::Array> aa, la;
    st = a.Finish(&aa);
    if (!st.ok()) throw std::runtime_error(st.ToString());
    st = label.Finish(&la);
    if (!st.ok()) throw std::runtime_error(st.ToString());
    auto schema = arrow::schema({arrow::field("a", arrow::int64()),
                                 arrow::field("label", arrow::utf8())});
    return arrow::Table::Make(schema, {aa, la});
}

int main(int argc, char** argv) {
    try {
        std::string mode = argc > 1 ? argv[1] : "";
        if (mode == "read" && argc >= 3) {
            auto t = synnodb::ReadArrowTableFromShm(argv[2]);
            long long sum = 0;
            auto col = t->column(0);
            for (int c = 0; c < col->num_chunks(); ++c) {
                auto arr = std::static_pointer_cast<arrow::Int64Array>(col->chunk(c));
                for (int64_t i = 0; i < arr->length(); ++i)
                    if (!arr->IsNull(i)) sum += arr->Value(i);
            }
            std::cout << "rows=" << t->num_rows() << " cols=" << t->num_columns()
                      << " col0=" << t->schema()->field(0)->name() << " sum0=" << sum << "\n";
            return 0;
        }
        if (mode == "write" && argc >= 4) {
            synnodb::WriteArrowTableToShm(make_table(std::stoll(argv[3])), argv[2]);
            std::cout << "wrote " << argv[3] << "\n";
            return 0;
        }
        std::cerr << "usage: shm_io_test read <path> | write <path> <n>\n";
        return 2;
    } catch (const std::exception& e) {
        std::cerr << "ERROR: " << e.what() << "\n";
        return 1;
    }
}
