#pragma once

#include "read_api.hpp"
#include "write_api.hpp"

struct IngestApi {
    BffWriteApi write;
    BffReadApi read;
};
