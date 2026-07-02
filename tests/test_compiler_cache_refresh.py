"""``build_plain`` is the publish gate's compile path (``force_compile=True`` in
``run_worker``): a real build that leaves the caching chain completely alone - no cache
read, no cache write. Writing back after a forced rebuild used to crash with
``FileExistsError`` on the already-existing key, aborting the publish of a fully validated
engine; silently rewriting the entry instead would have tampered with the cache.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from synnodb.cpp_runner.compiler.compiler import Compiler
from synnodb.cpp_runner.compiler.compiler_cached import CachedCompiler


@pytest.fixture
def compiler(tmp_path, monkeypatch):
    monkeypatch.setattr(Compiler, "build", lambda self: None)  # a successful "compile"
    args = dict(
        working_dir=tmp_path,
        libs={},
        main_src=Path("main.cpp"),
        app_extra_srcs=[],
        include_dirs=[],
    )
    comp = CachedCompiler(
        args=args, compile_cache_dir=tmp_path / "cache", untracked_cpp_runner_content=""
    )
    comp.set_compile_options(optimize=True, trace_mode=False)
    return comp


def test_build_plain_leaves_an_existing_cache_entry_untouched(compiler, tmp_path):
    # First build via the cached path: cache miss, entry written.
    out, from_cache, key = compiler.build_cached(current_git_snapshot="snap")
    assert out is None and from_cache is False
    entry = tmp_path / "cache" / f"{key}.pkl"
    before = entry.read_bytes()

    # Same key again: served from cache.
    _, from_cache, _ = compiler.build_cached(current_git_snapshot="snap")
    assert from_cache is True

    # The plain build must compile for real (never answer from cache), report the same
    # key for downstream consumers, and leave the cache entry byte-for-byte untouched.
    out, from_cache, key2 = compiler.build_plain(current_git_snapshot="snap")
    assert out is None and from_cache is False and key2 == key
    assert entry.read_bytes() == before


def test_build_plain_writes_no_cache_entry(compiler, tmp_path):
    out, from_cache, key = compiler.build_plain(current_git_snapshot="snap")
    assert out is None and from_cache is False and key
    assert not list((tmp_path / "cache").glob("*.pkl"))

    # The key build_plain reports is the one the cached path would use.
    _, _, cached_key = compiler.build_cached(current_git_snapshot="snap")
    assert cached_key == key
