"""_judge_storage_plan(): model resolution and stats-collector logging.

The judge is a one-off LLM completion outside the main conversation, so it's easy for
it to silently drift from how every other LLM call in the pipeline behaves. These tests
pin the fixed behaviour:
- it resolves api_base/api_key the same way the main conversation's model wrapper does
  (setup_model_config), instead of calling litellm with just a bare model string;
- it reports cost/verdict through ctx.run_tool.run_stats_collector instead of being
  invisible to cost/observability tracking;
- a call failure (bad endpoint, transient error) still degrades to "skip the check"
  rather than crashing the stage.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from synnodb.conversations.conv_context import ConvContext
from synnodb.conversations.examples.storage_plan import _judge_storage_plan
from synnodb.conversations.filenames import Filenames
from synnodb.utils.utils import DBStorage

SCHEMA = "CREATE TABLE lineitem (l_orderkey BIGINT);"
PLAN_TEXT = "l_orderkey stored as int64, sorted ascending, zone-mapped per 64k block."


def _fake_response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _make_ctx(run_stats_collector=None) -> ConvContext:
    run_tool = MagicMock(name="run_tool")
    run_tool.run_stats_collector = run_stats_collector or MagicMock(
        name="run_stats_collector"
    )
    return ConvContext(
        query_ids=["1"],
        filenames=Filenames.for_usecase(),
        workspace_path=MagicMock(name="workspace_path"),
        db_storage=DBStorage.IN_MEMORY,
        threads=1,
        model="openai/unsloth/MiniMax-M3",
        run_tool=run_tool,
        workload_provider=MagicMock(name="workload_provider"),
        sql_dict={},
        workload=None,
    )


def test_resolves_api_base_and_key_instead_of_bare_model_string(monkeypatch):
    """The exact bug from the review: a bare `litellm.completion(model=ctx.model)` call
    misses the self-hosted api_base/api_key that setup_model_config resolves, so it would
    silently try to hit the real provider's cloud endpoint for a local model name."""
    import synnodb.conversations.examples.storage_plan as sp

    monkeypatch.setattr(
        sp,
        "setup_model_config",
        lambda model_arg: (
            True,
            model_arg,
            "resolved-key",
            None,
            "http://local:1234/v1",
        ),
    )
    completion_calls = []

    def fake_completion(**kwargs):
        completion_calls.append(kwargs)
        return _fake_response("VALID")

    monkeypatch.setattr(sp.litellm, "completion", fake_completion)
    monkeypatch.setattr(sp.litellm, "completion_cost", lambda resp: 0.001)

    ctx = _make_ctx()
    result = _judge_storage_plan(ctx, SCHEMA, PLAN_TEXT)

    assert result is None  # VALID
    assert len(completion_calls) == 1
    assert completion_calls[0]["api_base"] == "http://local:1234/v1"
    assert completion_calls[0]["api_key"] == "resolved-key"


def test_reports_cost_and_verdict_to_stats_collector(monkeypatch):
    import synnodb.conversations.examples.storage_plan as sp

    monkeypatch.setattr(
        sp, "setup_model_config", lambda model_arg: (True, model_arg, "k", None, None)
    )
    monkeypatch.setattr(
        sp.litellm, "completion", lambda **kwargs: _fake_response("VALID")
    )
    monkeypatch.setattr(sp.litellm, "completion_cost", lambda resp: 0.0042)

    stats = MagicMock(name="run_stats_collector")
    ctx = _make_ctx(run_stats_collector=stats)

    _judge_storage_plan(ctx, SCHEMA, PLAN_TEXT)

    activity_entries = [c.args[0] for c in stats.add_to_activity_summary.call_args_list]
    assert any("VALID" in e and "0.0042" in e for e in activity_entries)


def test_call_failure_skips_the_check_without_crashing(monkeypatch):
    import synnodb.conversations.examples.storage_plan as sp

    monkeypatch.setattr(
        sp, "setup_model_config", lambda model_arg: (True, model_arg, "k", None, None)
    )

    def raising_completion(**kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(sp.litellm, "completion", raising_completion)

    ctx = _make_ctx()
    result = _judge_storage_plan(ctx, SCHEMA, PLAN_TEXT)

    assert result is None  # treated as "skip the check", not a crash
