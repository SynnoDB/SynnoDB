"""``CachedCompiler.build_cached`` cache semantics: the cache stores compiler verdicts, never
binaries, so ``skip_cache=True`` (a caller that needs a binary on disk, e.g. the publish gate)
must always build live - it overrides ``only_from_cache``. An only-from-cache replay in turn
never writes the cache, so its forced rebuilds cannot overwrite the recorded chain."""

from pathlib import Path

import pytest

from synnodb.cpp_runner.compiler.compiler import Compiler
from synnodb.cpp_runner.compiler.compiler_cached import CachedCompiler

SNAPSHOT = "deadbeef"


def _make_compiler(tmp_path, monkeypatch, calls, **kwargs) -> CachedCompiler:
    """A CachedCompiler over a stub live build that records each invocation in *calls*."""
    monkeypatch.setattr(Compiler, "build", lambda self: calls.append("build") or None)
    args = dict(
        working_dir=tmp_path,
        libs={},
        main_src=Path("main.cpp"),
        app_extra_srcs=[],
        include_dirs=[],
    )
    compiler = CachedCompiler(args, compile_cache_dir=tmp_path / "cache", **kwargs)
    compiler.set_compile_options(optimize=False, trace_mode=False)
    return compiler


def test_only_from_cache_raises_on_miss(tmp_path, monkeypatch):
    calls: list[str] = []
    compiler = _make_compiler(tmp_path, monkeypatch, calls, only_from_cache=True)
    with pytest.raises(Exception, match="only_from_cache"):
        compiler.build_cached(current_git_snapshot=SNAPSHOT)
    assert calls == []  # never built live


def test_only_from_cache_serves_a_hit(tmp_path, monkeypatch):
    calls: list[str] = []
    # A prior live run populates the cache entry for this snapshot.
    _make_compiler(tmp_path, monkeypatch, calls).build_cached(
        current_git_snapshot=SNAPSHOT
    )
    assert calls == ["build"]

    replay = _make_compiler(tmp_path, monkeypatch, calls, only_from_cache=True)
    output, from_cache, _ = replay.build_cached(current_git_snapshot=SNAPSHOT)
    assert output is None and from_cache is True
    assert calls == ["build"]  # served from cache, no second live build


def test_skip_cache_overrides_only_from_cache(tmp_path, monkeypatch):
    """The publish-gate case: a fully cache-replayed run has no binary on disk, and its
    forced rebuild (skip_cache=True) must compile live instead of raising."""
    calls: list[str] = []
    compiler = _make_compiler(tmp_path, monkeypatch, calls, only_from_cache=True)
    output, from_cache, _ = compiler.build_cached(
        skip_cache=True, current_git_snapshot=SNAPSHOT
    )
    assert output is None and from_cache is False
    assert calls == ["build"]


def test_only_from_cache_forced_rebuild_never_writes_the_cache(tmp_path, monkeypatch):
    calls: list[str] = []
    compiler = _make_compiler(tmp_path, monkeypatch, calls, only_from_cache=True)
    compiler.build_cached(skip_cache=True, current_git_snapshot=SNAPSHOT)
    assert not list((tmp_path / "cache").glob("*.pkl"))

    # The same forced rebuild in a normal (non-replay) run does write the entry.
    live = _make_compiler(tmp_path, monkeypatch, calls)
    live.build_cached(skip_cache=True, current_git_snapshot=SNAPSHOT)
    assert len(list((tmp_path / "cache").glob("*.pkl"))) == 1
