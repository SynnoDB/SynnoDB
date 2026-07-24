"""The compile/validate cache-chain assert in ``RunTool.run_query_batch``: a validation
replayed from cache normally implies the compile was served from cache too (they are keyed
to the same chain). A forced rebuild (``force_compile``, the publish gate) bypasses the
compile cache lookup entirely, so ``compile_used_cache=False`` carries no chain signal there
and must not trip the assert - the gate always pairs a live compile with cached validations."""

import types

import pytest

from synnodb.tools.run import RunTool, RunToolMode


class _ReplayValidator:
    """exec_and_validate double that always replays from the validation cache."""

    def exec_and_validate(self, **_):
        return types.SimpleNamespace(
            message="ok",
            success=True,
            metrics={},
            replayed_from_cache=True,
            trace_output=None,
            resp="",
            stdout="",
            stderr="",
            ingest_time_ms=None,
        )


def _make_run_tool(tmp_path) -> RunTool:
    rt = object.__new__(RunTool)  # bypass __init__: run_query_batch needs only a few attrs
    rt.cwd = str(tmp_path)
    rt.query_validator = _ReplayValidator()
    rt.memory_budget_mb = 256
    rt.parse_out_and_validate_output = True
    rt.run_stats_collector = None
    rt.validate_output_truncation = None
    return rt


def _make_batch(tag: str):
    return types.SimpleNamespace(
        cli_call_args=f"cache-chain-test-{tag}",
        general_system_config=types.SimpleNamespace(memory_limit_mb=256),
        extra_env=None,
        exec_settings=types.SimpleNamespace(data_source=None, scale_factor=1.0),
        query_list=[],
        timeout_s=1,
    )


def _run(rt, batch, *, compile_used_cache, force_compile):
    return rt.run_query_batch(
        batch,
        echo_output=False,
        compile_used_cache=compile_used_cache,
        current_git_snapshot=None,
        optimize=True,
        trace_mode=False,
        compile_key_hash="hash",
        general_extra_env={},
        external_call=False,
        current_parallelism=False,
        run_tool_mode=RunToolMode.EXHAUSTIVE,
        current_core_ids=None,
        current_num_threads=1,
        force_compile=force_compile,
    )


def test_replayed_validation_with_cache_missed_compile_still_asserts(tmp_path):
    rt = _make_run_tool(tmp_path)
    with pytest.raises(AssertionError, match="Inconsistent cache usage"):
        _run(rt, _make_batch("miss"), compile_used_cache=False, force_compile=False)


def test_forced_rebuild_with_replayed_validation_passes(tmp_path):
    """The publish gate's shape: force_compile=True makes compile_used_cache=False by
    construction while the validations legitimately replay from cache."""
    rt = _make_run_tool(tmp_path)
    result = _run(rt, _make_batch("forced"), compile_used_cache=False, force_compile=True)
    assert result.success is True
    assert result.metrics["validation/replayed_from_cache"] is True
