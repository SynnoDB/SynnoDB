"""Incremental /api/stats protocol for the live dashboard.

A long run accumulates thousands of turns; re-serializing and re-shipping the
whole store on every 3s poll is what made the browser (and the run's own
process) lag. These tests pin the delta protocol that fixes it: the client sends
its highest known step as ``?since=`` and the server returns only that step
onward, plus ``latest``/``count`` so the client can detect drift and refetch.
"""

import json
import threading

from synnodb.observability.live_ui.live_dashboard import (
    LiveDashboardDrain,
    _finalize_snapshot,
    _lazy_fields_payload,
    _parse_since,
    _slim_rows,
)


def _bare_drain() -> LiveDashboardDrain:
    """A LiveDashboardDrain with its data structures set up but no HTTP server."""
    d = LiveDashboardDrain.__new__(LiveDashboardDrain)
    d._data = {}
    d._lock = threading.Lock()
    d._rev = 0
    d._cache_full = None
    d._cache_rev = -1
    d._stage_base = 0
    d._carry = {}
    d._last_global = {}
    d._stages = []
    d._meta = {
        "run_name": None,
        "stages": d._stages,
        "planned_stages": None,
        "error": None,
        "num_threads": None,
    }
    d._workspace_dir = None
    return d


def test_parse_since_tolerates_missing_and_garbage():
    assert _parse_since(None) is None
    assert _parse_since("") is None
    assert _parse_since("not-a-number") is None
    assert _parse_since("7") == 7


def test_finalize_snapshot_full_and_delta_do_not_mutate_input():
    raw = {
        "meta": {},
        "steps": [0, 1, 2],
        "data": {"0": {"a": 1}, "1": {"a": 2}, "2": {"a": 3}},
    }
    full = json.loads(_finalize_snapshot(raw, None))
    assert full["latest"] == 2 and full["count"] == 3
    assert "incremental" not in full

    delta = json.loads(_finalize_snapshot(raw, 1))
    assert delta["incremental"] is True
    assert delta["steps"] == [1, 2]
    assert set(delta["data"]) == {"1", "2"}
    # latest/count still describe the *full* store, not the slice.
    assert delta["latest"] == 2 and delta["count"] == 3
    # The source dict is untouched (a cached source dict must survive slicing).
    assert set(raw["data"]) == {"0", "1", "2"}


def test_full_snapshot_carries_latest_and_count():
    d = _bare_drain()
    d.begin_stage(run_name="t")
    for step in range(4):
        d.emit({"type": "llm", "input_tokens": step}, step)

    snap = json.loads(d._snapshot())
    assert snap["count"] == 4
    assert snap["latest"] == 3
    assert snap["steps"] == [0, 1, 2, 3]
    assert "incremental" not in snap


def test_delta_returns_boundary_step_and_newer_only():
    d = _bare_drain()
    d.begin_stage(run_name="t")
    for step in range(4):
        d.emit({"type": "llm", "input_tokens": step}, step)

    # Client holds up to step 3; it re-requests from 3 so the (possibly still
    # accumulating) boundary step refreshes.
    delta = json.loads(d._snapshot(3))
    assert delta["incremental"] is True
    assert delta["steps"] == [3]
    assert set(delta["data"]) == {"3"}

    d.emit({"type": "shell", "input_tokens": 99}, 4)
    delta2 = json.loads(d._snapshot(3))
    assert delta2["steps"] == [3, 4]
    assert delta2["latest"] == 4 and delta2["count"] == 5


def test_full_snapshot_is_cached_until_the_next_mutation():
    d = _bare_drain()
    d.begin_stage(run_name="t")
    d.emit({"type": "llm", "input_tokens": 0}, 0)

    first = d._snapshot()
    assert d._snapshot() is first  # identical object → served from cache

    d.emit({"type": "llm", "input_tokens": 1}, 1)
    assert d._snapshot() is not first  # a new emit invalidates the cache


def test_reset_drops_count_so_client_detects_drift():
    d = _bare_drain()
    d.begin_stage(run_name="t")
    for step in range(3):
        d.emit({"type": "llm", "input_tokens": step}, step)
    assert json.loads(d._snapshot())["count"] == 3

    d._reset()
    after = json.loads(d._snapshot(2))
    # A client that still holds 3 steps sees count=0 and refetches from scratch.
    assert after["count"] == 0
    assert after["latest"] is None
    assert after["steps"] == []


def test_slim_rows_removes_lazy_fields_and_trims_floats_without_mutating_input():
    raw = {
        "0": {
            "type": "shell",
            "shell/commands": ["ls"],
            "shell/outputs": "a" * 5000,
            "total/runtime": 12.345678901234567,
            "wallclock_time": 1752291234.5678901,
        },
        "1": {"type": "llm", "input_tokens": 3, "llm/output_text": "hello" * 100},
    }
    slim = _slim_rows(raw)
    # Every lazy field is gone; everything else stays.
    assert "shell/outputs" not in slim["0"]
    assert "wallclock_time" not in slim["0"]
    assert slim["0"]["shell/commands"] == ["ls"]
    assert "llm/output_text" not in slim["1"]
    assert slim["1"]["input_tokens"] == 3
    # Full-precision doubles are trimmed for the wire.
    assert slim["0"]["total/runtime"] == 12.345679
    # The input is untouched (a cached source dict must survive slimming).
    assert raw["0"]["shell/outputs"] == "a" * 5000
    assert raw["0"]["total/runtime"] == 12.345678901234567


def test_finalize_snapshot_slims_rows_but_can_be_disabled_for_remote():
    raw = {
        "meta": {},
        "steps": [0],
        "data": {"0": {"type": "shell", "shell/outputs": "big", "shell/cached": True}},
    }
    stripped = json.loads(_finalize_snapshot(raw, None))
    assert "shell/outputs" not in stripped["data"]["0"]
    assert stripped["data"]["0"]["shell/cached"] is True  # non-lazy field kept

    passthrough = json.loads(_finalize_snapshot(raw, None, strip=False))
    assert passthrough["data"]["0"]["shell/outputs"] == "big"  # remote proxy verbatim


def test_lazy_fields_payload_returns_only_lazy_fields_for_the_step():
    data = {
        "0": {"type": "shell", "shell/commands": ["ls"], "shell/outputs": "OUT"},
        "1": {
            "type": "llm",
            "llm/output_text": "TEXT",
            "input_tokens": 2,
            "current_prompt": "PROMPT",
            "agent_config": '{"model": "m1"}',
        },
    }
    body0 = json.loads(_lazy_fields_payload(data, 0))
    assert body0 == {"step": "0", "fields": {"shell/outputs": "OUT"}}
    body1 = json.loads(_lazy_fields_payload(data, "1"))
    assert body1["fields"] == {
        "llm/output_text": "TEXT",
        "current_prompt": "PROMPT",
        "agent_config": '{"model": "m1"}',
    }
    # Unknown step → None (the endpoint replies 404).
    assert _lazy_fields_payload(data, 99) is None


def test_live_snapshot_strips_lazy_fields_and_body_endpoint_serves_them():
    d = _bare_drain()
    d.begin_stage(run_name="t")
    d.emit(
        {
            "type": "shell",
            "shell/commands": ["ls"],
            "shell/outputs": "X" * 4000,
            "llm_hash": "ab" * 20,
        },
        0,
    )

    snap = json.loads(d._snapshot())
    assert "shell/outputs" not in snap["data"]["0"]  # stripped from the feed
    assert "llm_hash" not in snap["data"]["0"]  # debug metadata stripped too
    assert "shell/commands" in snap["data"]["0"]  # summary field retained

    body = json.loads(d._lazy_fields("0"))
    assert body["fields"]["shell/outputs"] == "X" * 4000  # full text on demand
    assert body["fields"]["llm_hash"] == "ab" * 20
    assert d._lazy_fields("99") is None  # unknown step → 404
    assert d._lazy_fields("nan") is None  # unparseable step → 404


def test_live_snapshot_trims_float_precision():
    d = _bare_drain()
    d.begin_stage(run_name="t")
    d.emit({"type": "llm", "total/runtime": 12.345678901234567}, 0)
    snap = json.loads(d._snapshot())
    assert snap["data"]["0"]["total/runtime"] == 12.345679


def test_slim_rows_recurses_into_containers_and_neutralizes_nested_nan():
    # DuckDB-sourced rows can hold decoded JSON containers. Nested floats are
    # trimmed too, and a nested NaN must become null - json.dumps would emit it
    # as a bare NaN literal that the browser's JSON.parse rejects, killing the
    # poll loop.
    raw = {
        "0": {
            "type": "validate",
            "runs": [1.234567890123, float("nan"), {"ms": 9.876543210987}],
        }
    }
    slim = _slim_rows(raw)
    assert slim["0"]["runs"] == [1.234568, None, {"ms": 9.876543}]
    json.loads(json.dumps(slim))  # round-trips as strict JSON


def test_delta_omits_meta_only_when_client_rev_matches():
    d = _bare_drain()
    d.begin_stage(run_name="t")
    d.emit({"type": "llm", "input_tokens": 0}, 0)

    full = json.loads(d._snapshot())
    rev = full["meta_rev"]
    assert full["meta"]["run_name"] == "t"  # full snapshots always carry meta

    # Idle delta with the current rev: meta omitted, rev echoed.
    delta = json.loads(d._snapshot(0, rev))
    assert "meta" not in delta
    assert delta["meta_rev"] == rev

    # No rev / stale rev: meta included.
    assert "meta" in json.loads(d._snapshot(0))
    assert "meta" in json.loads(d._snapshot(0, "stale"))

    # A meta change (new stage) moves the rev, so the client's rev no longer
    # suppresses the block.
    d.begin_stage(run_name="t2")
    after = json.loads(d._snapshot(0, rev))
    assert after["meta"]["run_name"] == "t2"
    assert after["meta_rev"] != rev


def test_meta_model_lifted_from_agent_config():
    d = _bare_drain()
    d.begin_stage(run_name="t")
    d.emit({"type": "llm", "agent_config": json.dumps({"model": "m1"})}, 0)
    d.emit({"type": "shell"}, 1)
    assert json.loads(d._snapshot())["meta"]["model"] == "m1"

    # The newest config wins; agent_config itself stays out of the feed.
    d.emit({"type": "llm", "agent_config": json.dumps({"model": "m2"})}, 2)
    snap = json.loads(d._snapshot())
    assert snap["meta"]["model"] == "m2"
    assert "agent_config" not in snap["data"]["0"]


def test_finalize_snapshot_lifts_model_for_standalone_sources():
    raw = {
        "meta": {"run_name": "r"},
        "steps": [0, 1],
        "data": {
            "0": {"type": "llm", "agent_config": '{"model": "old"}'},
            "1": {"type": "llm", "agent_config": '{"model": "new"}'},
        },
    }
    out = json.loads(_finalize_snapshot(raw, None))
    assert out["meta"]["model"] == "new"  # newest step wins
    # The source dict's meta is untouched.
    assert "model" not in raw["meta"]


def test_reset_bumps_start_time_so_client_detects_generation_change():
    # The count guard alone can be fooled: if a new pipeline restarts and races
    # past the previous run's step count before the client polls, the delta plus
    # the client's stale steps sum to the same count and drift goes unnoticed. The
    # client instead keys off meta.start_time, which _reset always refreshes.
    d = _bare_drain()
    d._meta["start_time"] = "2020-01-01T00:00:00"
    d.begin_stage(run_name="a")
    for step in range(3):
        d.emit({"type": "llm", "input_tokens": step}, step)
    before = json.loads(d._snapshot())["meta"]["start_time"]

    d._reset()
    d.begin_stage(run_name="b")
    # The new run re-emits enough steps that the count matches the old count,
    # which is exactly the case the count guard cannot catch.
    for step in range(3):
        d.emit({"type": "llm", "input_tokens": step}, step)
    delta = json.loads(d._snapshot(2))
    assert delta["count"] == 3  # count parity - the count guard would not fire
    assert delta["meta"]["start_time"] != before  # but the generation changed
