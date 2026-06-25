#include "builder_api.hpp"
#include "parquet_reader.hpp"

#include <cstdlib>
#include <exception>
#include <filesystem>
#include <iostream>
#include <string>

namespace {

std::string with_trailing_slash(std::string path) {
    if (!path.empty() && path.back() != '/') path.push_back('/');
    return path;
}

}  // namespace

int main(int argc, char** argv) {
    const std::string parquet_dir = with_trailing_slash(argc > 1 ? argv[1] : "input_parquet");
    const std::string bff_dir = argc > 2 ? argv[2] : "bff_store";

    try {
        std::filesystem::create_directories(bff_dir);
        setenv("STORAGE_DIR", bff_dir.c_str(), 1);

        ParquetTables* tables = load(parquet_dir);
        Database* db = build(tables);

        destroy_database(db);
        destroy_parquet_tables(tables);

        std::cout << "Generated BFF files in " << bff_dir << "\n";
        return 0;
    } catch (const std::exception& ex) {
        std::cerr << "generate_bff failed: " << ex.what() << "\n";
        return 1;
    }
}
