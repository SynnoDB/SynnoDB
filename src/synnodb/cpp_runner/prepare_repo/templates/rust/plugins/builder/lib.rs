//! libbuilder.so - the C ABI shim for the builder plugin.
//!
//! Read-only scaffold. The Rust counterpart of api/olap/builder_api.cpp; see the
//! loader shim for why it is its own crate.

use std::ffi::{c_char, c_void};

use engine_builder::{build, Database};
use engine_loader::ParquetTables;
use synno_rt::abi::{guarded, last_error_ptr, SynnoBuilderApi};

extern "C" fn abi_build(tables: *mut c_void) -> *mut c_void {
    guarded("builder", || {
        // The loader allocated this and still owns it; we only read it.
        let tables = unsafe { &*(tables as *const ParquetTables) };
        build(tables).map(Box::new)
    })
}

extern "C" fn abi_destroy(db: *mut c_void) {
    if !db.is_null() {
        drop(unsafe { Box::from_raw(db as *mut Database) });
    }
}

extern "C" fn abi_last_error() -> *const c_char {
    last_error_ptr()
}

static BUILDER: SynnoBuilderApi = SynnoBuilderApi {
    build: abi_build,
    destroy: abi_destroy,
    last_error: abi_last_error,
};

#[no_mangle]
pub extern "C" fn plugin_query() -> *const c_void {
    &BUILDER as *const SynnoBuilderApi as *const c_void
}
