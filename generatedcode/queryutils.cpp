// queryutils.cpp — BFF read API implementation for the query library.
//
// The OLAP pipeline's query lib does not include read_impl.cpp by default.
// Including it here (via the DynamicQueryCompiler's glob discovery) makes the
// BFF read API symbols available to all queryN.cpp files at link time.
//
// FILE_VERSION: 1

// Pull in the full read implementation once, in this translation unit.
#include "read_impl.cpp"