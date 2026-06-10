#pragma once

#include <stdexcept>
#include <system_error>

// FILE_VERSION: 1

void pin_process_to_cpu(int core_id);
void unpin_process_from_cpus();
