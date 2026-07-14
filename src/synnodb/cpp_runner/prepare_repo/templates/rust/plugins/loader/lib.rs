//! libloader.so - the C ABI shim for the loader plugin.
//!
//! Read-only scaffold. The Rust counterpart of api/olap/loader_api.cpp, and its
//! own crate for the same reason that file is its own translation unit:
//! plugin_query is #[no_mangle], so it must be linked into exactly one .so.
//!
//! ParquetTables crosses the boundary as an opaque void* the host never
//! dereferences, and is freed by this plugin -- the host's allocator is not ours.

use std::ffi::{c_char, c_void, CStr};

use engine_loader::{load_tables, ParquetTables};
use synno_rt::abi::{guarded, last_error_ptr, SynnoLoaderApi};

extern "C" fn abi_load(dir: *const c_char) -> *mut c_void {
    let dir = if dir.is_null() {
        String::new()
    } else {
        unsafe { CStr::from_ptr(dir) }.to_string_lossy().into_owned()
    };
    // Converts a failure OR a panic into "return null, record the message"; the
    // host rethrows it on its own side. Nothing unwinds across this frame.
    guarded("loader", || load_tables(&dir))
}

extern "C" fn abi_destroy(tables: *mut c_void) {
    if !tables.is_null() {
        drop(unsafe { Box::from_raw(tables as *mut ParquetTables) });
    }
}

extern "C" fn abi_last_error() -> *const c_char {
    last_error_ptr()
}

static LOADER: SynnoLoaderApi = SynnoLoaderApi {
    load: abi_load,
    destroy: abi_destroy,
    last_error: abi_last_error,
};

/// The host dlsym's this on every plugin.
#[no_mangle]
pub extern "C" fn plugin_query() -> *const c_void {
    &LOADER as *const SynnoLoaderApi as *const c_void
}
