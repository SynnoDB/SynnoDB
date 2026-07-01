"""A3 end-to-end: killing the orchestrator leaves no surviving engine process tree.

The plan's A3 verification asks to SIGKILL the Python orchestrator mid-run and assert no ./db tree
survives. The unit tests prove the re-arm helper's branches and the cgroup sweep in isolation; this
proves the whole live chain: a real loader -> builder -> query pipeline (the synthetic soak engine,
built straight on pipeline.hpp) launched under PR_SET_PDEATHSIG. When the orchestrator dies, the
kernel must SIGKILL the top engine and the death must cascade through all three stages.

The test is made PR_SET_PDEATHSIG-load-bearing (not merely a pipe-EOF cascade): the test keeps the
engine's control-pipe WRITE end open itself, so when the intermediate orchestrator dies the engine
does NOT see EOF on its control pipe. The only thing that can then reap the top engine is
PR_SET_PDEATHSIG - so if the arming ever regressed, the engine would block forever and this test
would fail with survivors. An intermediate 'orchestrator' process (death_orchestrator.py) owns the
engine so the test can kill it without killing pytest itself.
"""
import os
import select
import signal
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

_MAGIC = 0x31525043  # ipc::MESSAGE_MAGIC
_ACTION_RUN = 1
_DONE_TOKEN_BYTES = 8

_SOAK_DIR = Path(__file__).parent / "soak_engine"
_HOTPATCH_DIR = Path(__file__).resolve().parents[1] / "src" / "synnodb" / "cpp_runner" / "hotpatch"
_CXX = os.environ.get("CXX", "g++")

pytestmark = pytest.mark.skipif(
    shutil.which(_CXX) is None or not sys.platform.startswith("linux") or not Path("/proc").exists(),
    reason="needs a C++ compiler and Linux /proc",
)


def _compile(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"compile failed: {' '.join(args)}\n{proc.stderr}")


@pytest.fixture(scope="module")
def soak_build(tmp_path_factory) -> Path:
    build_dir = tmp_path_factory.mktemp("soak_death")
    _compile([
        _CXX, "-O2", "-std=c++20", "-I", str(_HOTPATCH_DIR),
        "-o", str(build_dir / "db_soak_host"),
        str(_SOAK_DIR / "host_soak.cpp"), str(_HOTPATCH_DIR / "build_id.cpp"), "-ldl",
    ])
    for name in ("loader", "query"):
        _compile([
            _CXX, "-O2", "-std=c++20", "-shared", "-fPIC", "-I", str(_SOAK_DIR),
            "-Wl,--build-id", "-o", str(build_dir / f"lib{name}_soak.so"),
            str(_SOAK_DIR / f"{name}_soak.cpp"),
        ])
    _compile([
        _CXX, "-O2", "-std=c++20", "-shared", "-fPIC", "-I", str(_SOAK_DIR),
        f"-Wl,--build-id=0x{0:040x}", "-o", str(build_dir / "libbuilder_soak.so"),
        str(_SOAK_DIR / "builder_soak.cpp"),
    ])
    return build_dir


def _descendants(root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            data = Path(f"/proc/{entry}/stat").read_text()
            ppid = int(data[data.rindex(")") + 2:].split()[1])
        except (OSError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    out, stack = [], [root]
    while stack:
        pid = stack.pop()
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def _running(pid: int) -> bool:
    """Alive and not a zombie. A killed process may briefly linger as a zombie until its reaper
    collects it; that is dead, not a surviving engine, so treat it as not running."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
        state = data[data.rindex(")") + 2:].split()[0]
        return state != "Z"
    except (OSError, ValueError):
        return False


def _kill_tree(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def test_orchestrator_death_reaps_the_engine_tree(soak_build):
    # The test owns both control pipes and keeps p2c_w / c2p_r; the orchestrator + engine get only
    # p2c_r / c2p_w. So when the orchestrator dies, the engine's control-pipe write end stays open
    # (held here), it never sees EOF, and only PR_SET_PDEATHSIG can reap it.
    p2c_r, p2c_w = os.pipe()
    c2p_r, c2p_w = os.pipe()
    orch = subprocess.Popen(
        [sys.executable, str(_SOAK_DIR / "death_orchestrator.py"),
         str(soak_build), str(p2c_r), str(c2p_w)],
        pass_fds=(p2c_r, c2p_w), stdout=subprocess.PIPE, text=True,
    )
    os.close(p2c_r)
    os.close(c2p_w)
    engine_pid = None
    tree: list[int] = []
    try:
        # The orchestrator prints the engine's top pid once it is launched.
        ready, _, _ = select.select([orch.stdout], [], [], 60.0)
        assert ready, "orchestrator never reported the engine pid (engine failed to start)"
        line = orch.stdout.readline()
        assert line.strip(), "orchestrator exited without reporting an engine pid"
        engine_pid = int(line.strip())

        # Drive one RUN so the loader forks the builder and the builder forks the query.
        os.write(p2c_w, struct.pack("<IIQII", _MAGIC, _ACTION_RUN, 1, 0, 0))
        buf = b""
        while len(buf) < _DONE_TOKEN_BYTES:
            ready, _, _ = select.select([c2p_r], [], [], 30.0)
            assert ready, "engine did not complete the run"
            chunk = os.read(c2p_r, _DONE_TOKEN_BYTES - len(buf))
            assert chunk, "done pipe closed before the run completed"
            buf += chunk

        # The loader -> builder -> query chain is now up (top + 2 forked stages).
        deadline = time.time() + 10.0
        while time.time() < deadline:
            tree = _descendants(engine_pid)
            if len(tree) >= 3:
                break
            time.sleep(0.05)
        assert len(tree) >= 3, f"engine chain did not fork its stages: {tree}"
        assert all(_running(pid) for pid in tree), f"engine tree not fully live: {tree}"

        # Kill the orchestrator. The engine's control-pipe write end is still held here, so the
        # engine sees no EOF - only PR_SET_PDEATHSIG can reap it, and the death must cascade.
        orch.send_signal(signal.SIGKILL)
        orch.wait(timeout=10)

        deadline = time.time() + 15.0
        survivors = tree
        while time.time() < deadline:
            survivors = [pid for pid in tree if _running(pid)]
            if not survivors:
                break
            time.sleep(0.05)
        assert not survivors, f"engine processes survived orchestrator death (orphaned): {survivors}"
    finally:
        for fd in (p2c_w, c2p_r):
            try:
                os.close(fd)
            except OSError:
                pass
        if orch.poll() is None:
            orch.send_signal(signal.SIGKILL)
            try:
                orch.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        if orch.stdout is not None:
            orch.stdout.close()
        if engine_pid is not None:
            _kill_tree(_descendants(engine_pid))
