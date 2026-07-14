//! The Rust side of the plugin ABI (`cpp_runner/api/plugin_abi.h`).
//!
//! The host (db.cpp + hotpatch/) is language-agnostic: it dlopens three .so
//! files, calls `plugin_query()` on each to get a struct of function pointers,
//! and passes opaque handles from stage to stage without ever dereferencing
//! them. These are those structs, `#[repr(C)]`, so a Rust cdylib is a plugin the
//! host cannot tell apart from the C++ one.
//!
//! Two rules the host relies on, and neither is checkable at compile time:
//!
//! * **Nothing may unwind across this boundary.** A panic escaping an
//!   `extern "C"` frame is undefined behaviour. Every entry point below catches
//!   it and reports the failure as data, which is exactly what the C++ shims do.
//! * **A handle is freed by the plugin that allocated it.** The host's allocator
//!   is not the plugin's, so each api struct pairs its producer with a destroy.

use std::ffi::{c_char, c_void, CStr, CString};
use std::panic::{catch_unwind, AssertUnwindSafe};

/// One query's outcome. Strings are plugin-owned and freed by `free_results`.
#[repr(C)]
pub struct SynnoQueryResult {
    pub query_id: *const c_char,
    pub req_id: *const c_char,
    pub trace: *const c_char,
    pub error: *const c_char,
    pub elapsed_ms: f64,
}

/// The outcome of one RUN batch. `stage_error` is non-empty when the batch failed
/// as a whole, as opposed to an individual query failing (that goes in the
/// query's own `error`).
#[repr(C)]
pub struct SynnoQueryBatchResult {
    pub results: *mut SynnoQueryResult,
    pub len: usize,
    pub stage_error: *const c_char,
}

#[repr(C)]
pub struct SynnoLoaderApi {
    pub load: extern "C" fn(*const c_char) -> *mut c_void,
    pub destroy: extern "C" fn(*mut c_void),
    pub last_error: extern "C" fn() -> *const c_char,
}

#[repr(C)]
pub struct SynnoBuilderApi {
    pub build: extern "C" fn(*mut c_void) -> *mut c_void,
    pub destroy: extern "C" fn(*mut c_void),
    pub last_error: extern "C" fn() -> *const c_char,
}

#[repr(C)]
pub struct SynnoQueryApi {
    pub query: extern "C" fn(*mut c_void, *const *const c_char, usize, *mut SynnoQueryBatchResult),
    pub free_results: extern "C" fn(*mut SynnoQueryBatchResult),
}

// Sound because these hold only fn pointers, which are immutable and thread-safe;
// the statics they live in are never mutated after construction.
unsafe impl Sync for SynnoLoaderApi {}
unsafe impl Sync for SynnoBuilderApi {}
unsafe impl Sync for SynnoQueryApi {}

// ---- helpers for the generated plugins --------------------------------------

/// Leak a Rust string as a C string the host can read until `free_results`.
/// A NUL byte in the middle cannot be represented, so it is stripped rather than
/// failing the whole batch (it can only come from data, never from control flow).
pub fn to_c_string(s: &str) -> *const c_char {
    let cleaned: String = s.chars().filter(|c| *c != '\0').collect();
    CString::new(cleaned)
        .unwrap_or_default()
        .into_raw() as *const c_char
}

/// Reclaim a string handed out by [`to_c_string`].
///
/// # Safety
/// `p` must have come from `to_c_string` and not been freed already.
pub unsafe fn free_c_string(p: *const c_char) {
    if !p.is_null() {
        drop(CString::from_raw(p as *mut c_char));
    }
}

/// Read the host's query lines.
///
/// # Safety
/// `lines` must point to `n` valid NUL-terminated strings, as the ABI promises.
pub unsafe fn query_lines(lines: *const *const c_char, n: usize) -> Vec<String> {
    (0..n)
        .map(|i| {
            let p = *lines.add(i);
            if p.is_null() {
                String::new()
            } else {
                CStr::from_ptr(p).to_string_lossy().into_owned()
            }
        })
        .collect()
}

thread_local! {
    static LAST_ERROR: std::cell::RefCell<CString> =
        std::cell::RefCell::new(CString::default());
}

pub fn set_last_error(msg: &str) {
    let cleaned: String = msg.chars().filter(|c| *c != '\0').collect();
    LAST_ERROR.with(|e| {
        *e.borrow_mut() = CString::new(cleaned).unwrap_or_default();
    });
}

/// The plugin-owned message for the most recent failure. Valid until the next
/// call into this plugin, and never null.
pub fn last_error_ptr() -> *const c_char {
    LAST_ERROR.with(|e| e.borrow().as_ptr())
}

/// Run a producer that may fail or panic, converting both into
/// "return null, record the message" -- the loader/builder failure protocol. The
/// host rethrows on its own side, so the error text survives without an unwind
/// ever crossing the boundary.
pub fn guarded<T, F>(what: &str, f: F) -> *mut c_void
where
    F: FnOnce() -> Result<Box<T>, crate::Error>,
{
    set_last_error("");
    match catch_unwind(AssertUnwindSafe(f)) {
        Ok(Ok(v)) => Box::into_raw(v) as *mut c_void,
        Ok(Err(e)) => {
            set_last_error(&format!("{what}: {e}"));
            std::ptr::null_mut()
        }
        Err(p) => {
            set_last_error(&format!("{what} panicked: {}", panic_message(&p)));
            std::ptr::null_mut()
        }
    }
}

pub fn panic_message(p: &Box<dyn std::any::Any + Send>) -> String {
    if let Some(s) = p.downcast_ref::<&str>() {
        (*s).to_string()
    } else if let Some(s) = p.downcast_ref::<String>() {
        s.clone()
    } else {
        "unknown panic".to_string()
    }
}
