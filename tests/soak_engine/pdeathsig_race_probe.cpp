// Probe for detail::rearm_pdeathsig_or_exit (A3): a forked stage child must not run orphaned when
// its intended parent already exited in the fork->prctl window. This exercises both branches of the
// helper deterministically:
//
//   (no arg)  intended_parent == the real parent -> the helper accepts and returns; the child
//             reaches _exit(0), so the program exits 0.
//   "race"    intended_parent is a pid the child's real parent can never be -> getppid() mismatches,
//             so the helper _exit()s with kParentDeathSetupFailedExitCode before returning.
//
// The program propagates the child's exit code, so the Python test can assert on it.

#include "pipeline.hpp"

#include <string>
#include <sys/wait.h>
#include <unistd.h>

int main(int argc, char** argv) {
    const bool simulate_race = (argc > 1 && std::string(argv[1]) == "race");
    const pid_t real_parent = getpid();

    pid_t pid = fork();
    if (pid == 0) {
        // A pid the real parent can never have (well past PID_MAX) forces the getppid() mismatch.
        const pid_t intended = simulate_race ? static_cast<pid_t>(1 << 30) : real_parent;
        detail::rearm_pdeathsig_or_exit(intended);
        _exit(0);  // reached only when the helper accepted the (correct) parent
    }
    if (pid < 0) {
        return 201;
    }
    int status = 0;
    if (waitpid(pid, &status, 0) < 0) {
        return 202;
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    return 200;  // killed by a signal - not expected in this probe
}
