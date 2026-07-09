"""ValidateMultiThreadedStage.next_prompt(): per-query multi-threaded correctness gate.

In-memory base impls are now generated and validated at the serving parallelism, but that
per-query validation goes through the correctness cache - and a data race is
nondeterministic, so a lucky pass can be cached and hide the race. This stage is the
authoritative force-live catch: it runs each query at `num_threads` (>1) and, for any
query whose result diverges under threads, loops the LLM to fix that query - re-validating
at the same thread count after every edit and giving up loudly after `MAX_FIX_ATTEMPTS`.
These tests pin that behaviour with a mocked run_tool.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from synnodb.conversations.conversation_engine import ValidationStillFailsException
from synnodb.conversations.examples.base_impl import ValidateMultiThreadedStage
from synnodb.tools.run import RunWorkerResult
from synnodb.tools.run_tool_mode import RunToolMode

NUM_THREADS = 4


def _make_stage(run_tool, *, num_threads=NUM_THREADS, query_ids=("1", "6")):
    return ValidateMultiThreadedStage(
        run_tool=run_tool,
        num_threads=num_threads,
        query_ids=list(query_ids),
        builder_path="db_loader.cpp",
    )


def _ok() -> RunWorkerResult:
    return RunWorkerResult(msg="ok", success=True)


def _diverge(msg: str = "Q6 sf20: expected sum=1.0 got 2.0") -> RunWorkerResult:
    # What run_worker returns for a query whose output differs from the reference.
    return RunWorkerResult(msg=msg, success=False)


def test_all_queries_correct_advances_without_prompt():
    """Every query correct at N threads: one run per query, no LLM turn, stage ends."""
    run_tool = MagicMock()
    run_tool.run_worker.return_value = _ok()
    stage = _make_stage(run_tool, query_ids=("1", "6"))

    assert stage.next_prompt() is None
    assert run_tool.run_worker.call_count == 2  # one run per query
    # The gate runs at the run's DEFAULT thread count (the serving target); the run tool is
    # already at its default here, so the gate sets no per-stage override.
    run_tool.set_active_num_threads.assert_not_called()
    kwargs = run_tool.run_worker.call_args.kwargs
    assert kwargs["mode"] == RunToolMode.EXHAUSTIVE
    assert kwargs["query_ids"] == ["6"]  # last query, checked in isolation
    # Never replay a cached (possibly lucky) verdict for a nondeterministic race.
    assert kwargs["force_live"] is True


def test_diverging_query_returns_scoped_fix_prompt_then_advances():
    """A query correct serially but wrong at N threads gets a fix prompt scoped to it,
    is re-validated at N threads, and the walk advances once it passes."""
    run_tool = MagicMock()
    # q1 ok; q6 diverges once, then ok after the "fix".
    run_tool.run_worker.side_effect = [_ok(), _diverge(), _ok()]
    stage = _make_stage(run_tool, query_ids=("1", "6"))

    prompt = stage.next_prompt()
    assert prompt is not None
    assert "query6.cpp" in prompt  # per-query prompt names the offending query file
    assert stage.idx == 1 and stage.fix_attempts == 1
    assert run_tool.run_worker.call_args.kwargs["query_ids"] == ["6"]

    # After the edit, the re-check passes and the stage finishes.
    assert stage.next_prompt() is None
    assert run_tool.run_worker.call_count == 3
    assert stage.fix_attempts == 0


def test_persistent_divergence_gives_up_after_max_attempts():
    """A query that never becomes correct at N threads raises after MAX_FIX_ATTEMPTS,
    with the last error surfaced in the message."""
    run_tool = MagicMock()
    run_tool.run_worker.return_value = _diverge("persistent race divergence")
    stage = _make_stage(run_tool, query_ids=("6",))

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


def test_single_core_host_is_a_noop():
    """Defensive: with only one usable core the engine cannot run multi-threaded, so the
    stage does nothing rather than validating a serial run."""
    run_tool = MagicMock()
    stage = _make_stage(run_tool, num_threads=1, query_ids=("1", "6"))

    assert stage.next_prompt() is None
    run_tool.run_worker.assert_not_called()
