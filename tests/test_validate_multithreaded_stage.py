"""ValidateMultiThreadedStage.next_prompt(): per-query multi-threaded correctness gate.

The base impl is generated and validated serially (CORE_IDS=1), so a query's parallel
`parallel_for`/`parallel_reduce` code is never exercised until the engine is served at
`config={'threads': N}`. This stage runs each query at several thread counts - 1, 8, the
largest prime <= N, and N (deduped, oversubscription clamped) - and, for any query whose
result diverges under threads, loops the LLM to fix that query, re-validating at the same
thread count after every edit and giving up loudly after `MAX_FIX_ATTEMPTS`. These tests
pin that behaviour with a mocked run_tool.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from synnodb.conversations.conversation_engine import ValidationStillFailsException
from synnodb.conversations.examples.base_impl import ValidateMultiThreadedStage
from synnodb.tools.run import RunWorkerResult
from synnodb.tools.run_tool_mode import RunToolMode


def _make_stage(
    run_tool,
    *,
    serving_core_ids=(0, 1, 2, 3),
    available_core_ids=(0, 1, 2, 3, 4, 5, 6, 7),
    query_ids=("1", "6"),
):
    return ValidateMultiThreadedStage(
        run_tool=run_tool,
        serving_core_ids=list(serving_core_ids),
        available_core_ids=list(available_core_ids),
        query_ids=list(query_ids),
        builder_path="db_loader.cpp",
    )


def _ok() -> RunWorkerResult:
    return RunWorkerResult(msg="ok", success=True)


def _diverge(msg: str = "Q6 sf20: expected sum=1.0 got 2.0") -> RunWorkerResult:
    # What run_worker returns for a query whose output differs from the reference.
    return RunWorkerResult(msg=msg, success=False)


def test_thread_count_matrix_is_derived_from_serving_parallelism():
    """N=4 serving cores on an 8-core host -> validate at 1, the largest prime <= 4 (3),
    4, and 8."""
    stage = _make_stage(MagicMock())
    assert stage.thread_counts == [1, 3, 4, 8]


def test_all_queries_correct_advances_without_prompt():
    """Every query correct at every thread count: one run per (count, query) pair, no
    LLM turn, and the run tool is left pinned to the serving parallelism."""
    run_tool = MagicMock()
    run_tool.run_worker.return_value = _ok()
    stage = _make_stage(run_tool, query_ids=("1", "6"))

    assert stage.next_prompt() is None
    # 4 thread counts x 2 queries.
    assert run_tool.run_worker.call_count == len(stage.thread_counts) * 2
    # The stage flips the run tool to multi-threaded and, when the whole matrix passes,
    # leaves it pinned to the serving parallelism for the stages that follow.
    assert run_tool.parallelism is True
    assert run_tool.core_ids == [0, 1, 2, 3]
    kwargs = run_tool.run_worker.call_args.kwargs
    assert kwargs["mode"] == RunToolMode.EXHAUSTIVE
    assert kwargs["query_ids"] == ["6"]  # last query, checked in isolation
    # Never replay a cached (possibly lucky) verdict for a nondeterministic race.
    assert kwargs["force_live"] is True


def test_diverging_query_returns_scoped_fix_prompt_then_advances():
    """A query correct serially but wrong at a higher thread count gets a fix prompt
    scoped to it, is re-validated at that count, and the walk advances once it passes."""
    run_tool = MagicMock()
    # Two thread counts (1, 2): q6 ok at 1 thread, diverges once at 2 threads, then ok.
    run_tool.run_worker.side_effect = [_ok(), _diverge(), _ok()]
    stage = _make_stage(
        run_tool,
        serving_core_ids=(0, 1),
        available_core_ids=(0, 1),
        query_ids=("6",),
    )
    assert stage.thread_counts == [1, 2]

    prompt = stage.next_prompt()
    assert prompt is not None
    assert "query6.cpp" in prompt  # per-query prompt names the offending query file
    assert stage.tc_idx == 1 and stage.fix_attempts == 1
    assert run_tool.parallelism is True  # run tool switched to the 2-thread count
    assert run_tool.core_ids == [0, 1]
    assert run_tool.run_worker.call_args.kwargs["query_ids"] == ["6"]

    # After the edit, the re-check passes and the stage finishes.
    assert stage.next_prompt() is None
    assert run_tool.run_worker.call_count == 3
    assert stage.fix_attempts == 0


def test_persistent_divergence_gives_up_after_max_attempts():
    """A query that never becomes correct at a thread count raises after
    MAX_FIX_ATTEMPTS, with the last error surfaced in the message."""
    run_tool = MagicMock()
    run_tool.run_worker.return_value = _diverge("persistent race divergence")
    stage = _make_stage(
        run_tool,
        serving_core_ids=(0, 1),
        available_core_ids=(0, 1),
        query_ids=("6",),
    )

    for _ in range(ValidateMultiThreadedStage.MAX_FIX_ATTEMPTS):
        assert stage.next_prompt() is not None

    with pytest.raises(
        ValidationStillFailsException, match="persistent race divergence"
    ):
        stage.next_prompt()

    assert (
        run_tool.run_worker.call_count
        == ValidateMultiThreadedStage.MAX_FIX_ATTEMPTS + 1
    )


def test_internal_probe_widths_capped_to_available_cores():
    """The internal probe width 8 is capped to the usable cores (here 4) so the stage
    never asks for more workers than there are cores. This capping is silent - the
    oversubscription warning is reserved for the user's own thread selection."""
    stage = _make_stage(
        MagicMock(),
        serving_core_ids=(0, 1, 2, 3),
        available_core_ids=(0, 1, 2, 3),
    )

    # 8 caps to 4; result is 1, largest prime <= 4 (3), and 4 - never 8.
    assert stage.thread_counts == [1, 3, 4]
    assert 8 not in stage.thread_counts


def test_single_core_host_is_a_noop():
    """Defensive: with only one usable core every count collapses to 1, so there is no
    parallelism to validate and the stage does nothing rather than validating a serial
    run."""
    run_tool = MagicMock()
    stage = _make_stage(
        run_tool,
        serving_core_ids=(0,),
        available_core_ids=(0,),
        query_ids=("1", "6"),
    )

    assert stage.thread_counts == [1]
    assert stage.next_prompt() is None
    run_tool.run_worker.assert_not_called()
