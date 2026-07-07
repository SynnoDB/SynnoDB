"""OptimizeBuildStage.next_prompt(): retry-then-give-up on a broken ingest build.

The preceding "base impl storage" stage has no gate confirming db_loader.cpp actually
compiles before handing off to OptimizeBuildStage, so seeing a failed ingest/compile on
entry is a normal, expected state - not a reason to crash the whole conversation. These
tests pin the fixed behaviour: a failed run_worker() call is fed back to the LLM as a
retry prompt (up to MAX_INGEST_FIX_ATTEMPTS times), and only sustained failure raises
ValidationStillFailsException, instead of the previous unconditional
`assert run_result.ingest_time_ms is not None`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from synnodb.conversations.conversation_engine import ValidationStillFailsException
from synnodb.conversations.examples.base_impl import OptimizeBuildStage
from synnodb.tools.run import RunWorkerResult
from synnodb.tools.run_tool_mode import RunToolMode
from synnodb.workloads.workload_provider import ExecSettings

COMPILE_ERROR = "db_loader.cpp:42: error: 'CrossCsr' was not declared in this scope"


def _make_stage(run_tool) -> OptimizeBuildStage:
    return OptimizeBuildStage(
        builder_path_cpp="db_loader.cpp",
        builder_path_hpp="db_loader.hpp",
        run_tool=run_tool,
        persistent_storage=False,
        allow_storage_restructuring=True,
        storage_plan_filename="storage_plan.txt",
        base_impl_todo_filename="base_impl_todo.txt",
        num_threads=1,
    )


def _success_result() -> RunWorkerResult:
    return RunWorkerResult(
        msg="ok",
        success=True,
        ingest_time_ms=5541.0,
        query_batch=SimpleNamespace(exec_settings=ExecSettings()),
    )


def _failure_result(err: str = COMPILE_ERROR) -> RunWorkerResult:
    # Mirrors what run_worker() actually returns on a compile error (run.py's
    # compile-error branch): success=False, ingest_time_ms left at its default None.
    return RunWorkerResult(msg=err, success=False, err=err)


def test_succeeds_first_try_returns_optimize_prompt_and_stops():
    run_tool = MagicMock(memory_budget_mb=16384)
    run_tool.run_worker.return_value = _success_result()
    stage = _make_stage(run_tool)

    prompt = stage.next_prompt()

    assert prompt is not None
    assert (
        "db_loader.cpp" in prompt
    )  # the base_optimize_build prompt names the builder file
    run_tool.run_worker.assert_called_once()
    assert run_tool.run_worker.call_args.kwargs["mode"] == RunToolMode.INGEST

    # Done after one success: no further ingest runs, no more prompts.
    assert stage.next_prompt() is None
    run_tool.run_worker.assert_called_once()


def test_compile_failure_retries_with_error_fed_back_instead_of_crashing():
    run_tool = MagicMock(memory_budget_mb=16384)
    run_tool.run_worker.side_effect = [_failure_result(), _success_result()]
    stage = _make_stage(run_tool)

    retry_prompt = stage.next_prompt()
    assert retry_prompt is not None
    assert COMPILE_ERROR in retry_prompt
    assert stage.executed is False
    assert stage.ingest_fix_attempts == 1

    # Second call re-attempts the ingest run; this time it succeeds and the stage finishes.
    final_prompt = stage.next_prompt()
    assert final_prompt is not None
    assert final_prompt != retry_prompt
    assert stage.executed is True
    assert run_tool.run_worker.call_count == 2


def test_ingest_time_none_despite_success_is_treated_as_failure():
    """Defensive: success=True with no ingest_time_ms must still retry, not crash."""
    run_tool = MagicMock(memory_budget_mb=16384)
    run_tool.run_worker.return_value = RunWorkerResult(
        msg="ok", success=True, ingest_time_ms=None
    )
    stage = _make_stage(run_tool)

    prompt = stage.next_prompt()

    assert prompt is not None
    assert stage.executed is False
    assert stage.ingest_fix_attempts == 1


def test_gives_up_after_max_attempts_with_last_error_in_message():
    run_tool = MagicMock(memory_budget_mb=16384)
    run_tool.run_worker.return_value = _failure_result("persistent compile error")
    stage = _make_stage(run_tool)

    for _ in range(OptimizeBuildStage.MAX_INGEST_FIX_ATTEMPTS):
        prompt = stage.next_prompt()
        assert prompt is not None

    with pytest.raises(ValidationStillFailsException, match="persistent compile error"):
        stage.next_prompt()

    assert (
        run_tool.run_worker.call_count == OptimizeBuildStage.MAX_INGEST_FIX_ATTEMPTS + 1
    )
