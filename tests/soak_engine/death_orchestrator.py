"""Thin intermediate 'orchestrator' for the A3 end-to-end death test.

It stands in for the Python generation orchestrator: it launches the synthetic soak engine as its
direct child with PR_SET_PDEATHSIG armed against itself (mirroring how the production launcher /
preexec ties the top ./db to the orchestrator), prints the engine's top pid, then blocks.

The engine's control-pipe fds are inherited from the parent test and passed straight through. The
test keeps the *other* ends of those pipes open, so the engine never sees EOF when this process
dies - the only thing that can then reap it is PR_SET_PDEATHSIG firing, which is exactly what the
test checks. When the test SIGKILLs this process, the engine must die and the death must cascade
down the stage tree.
"""
import ctypes
import os
import signal
import subprocess
import sys
import time

_PR_SET_PDEATHSIG = 1


def _arm_pdeathsig() -> None:
    # Runs between fork and exec in the engine child: arm PR_SET_PDEATHSIG=SIGKILL against this
    # process (the child's parent), so the engine dies when this orchestrator dies.
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        os._exit(87)


def main() -> None:
    build_dir = sys.argv[1]
    p2c_r = int(sys.argv[2])  # engine reads control here (test holds the write end)
    c2p_w = int(sys.argv[3])  # engine writes done here (test holds the read end)
    env = dict(
        os.environ,
        SOAK_READ_FD=str(p2c_r),
        SOAK_DONE_FD=str(c2p_w),
        SOAK_PID_FILE=os.path.join(build_dir, "builder.pid"),
        SOAK_HOG_MB="4",
        SOAK_INPUT_MB="4",
    )
    proc = subprocess.Popen(
        [os.path.join(build_dir, "db_soak_host")],
        cwd=build_dir,
        env=env,
        pass_fds=(p2c_r, c2p_w),
        preexec_fn=_arm_pdeathsig,
        stderr=subprocess.DEVNULL,
    )
    print(proc.pid, flush=True)  # hand the engine's top pid to the test
    while True:
        time.sleep(3600)  # stay alive as the engine's parent until the test SIGKILLs us


if __name__ == "__main__":
    main()
