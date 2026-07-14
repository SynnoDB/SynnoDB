#include "db_usecase.hpp"

// Minimal db use-case for the child-start-failure end-to-end test (A6). Linked
// with the real db.cpp so the test drives the actual run_parent / control-pipe
// path, not a stand-in. The single stage's next_start returns the invalid
// ChildHandle a real fork()/pipe() failure produces, so the child never starts
// and do_run must emit the kChildStartFailedExitCode done token; run_parent must
// then unblock (no trace is ever written, yet the child holds the trace pipe
// open) and report the failure to the caller instead of hanging.
//
// g_database stays null - the query stage never runs. It is an opaque handle in
// the plugin ABI (api/plugin_abi.h), so no Database type is needed here at all.

void* g_database = nullptr;

bool usecase_parse_args(int, char**) { return true; }

void usecase_run_child(int read_fd, int done_fd) {
    auto failing_next_start = [](auto /*output*/, int /*read_fd*/) -> detail::ChildHandle {
        return detail::ChildHandle{};  // pid == -1 -> !valid()
    };
    stage_loop_impl<RunPolicy::OnChange, false>(
        read_fd,
        done_fd,
        "./libloader_soak.so",
        "./libbuilder_soak.so",  // next_so_path; irrelevant on the failure path
        false,                    // restart_child_on_change
        detail::NoInput{},
        [](Plugin&) { return 0; },
        nullptr,                  // teardown
        failing_next_start,
        false);                   // start_now
}
