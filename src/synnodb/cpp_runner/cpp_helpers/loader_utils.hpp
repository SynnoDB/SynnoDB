#pragma once

#include <arrow/table.h>
#include <string>
#include <vector>

// FILE_VERSION: 2

std::shared_ptr<arrow::Table> ReadParquetTable(const std::string& path);
int NumParquetRowGroups(const std::string& path);
std::shared_ptr<arrow::Table> ReadParquetRowGroup(
    const std::string& path,
    int row_group,
    const std::vector<int>& column_indices = {});
