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
    _body_fields_payload,
    _finalize_snapshot,
    _parse_since,
    _strip_bodies,
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


def test_strip_bodies_removes_only_body_fields_without_mutating_input():
    raw = {
        "0": {"type": "shell", "shell/commands": ["ls"], "shell/outputs": "a" * 5000},
        "1": {"type": "llm", "input_tokens": 3, "llm/output_text": "hello" * 100},
    }
    stripped = _strip_bodies(raw)
    # Every heavy body field is gone; everything else stays.
    assert "shell/outputs" not in stripped["0"]
    assert stripped["0"]["shell/commands"] == ["ls"]
    assert "llm/output_text" not in stripped["1"]
    assert stripped["1"]["input_tokens"] == 3
    # The input is untouched (a cached source dict must survive stripping).
    assert raw["0"]["shell/outputs"] == "a" * 5000


def test_finalize_snapshot_strips_bodies_but_can_be_disabled_for_remote():
    raw = {
        "meta": {},
        "steps": [0],
        "data": {"0": {"type": "shell", "shell/outputs": "big", "shell/cached": True}},
    }
    stripped = json.loads(_finalize_snapshot(raw, None))
    assert "shell/outputs" not in stripped["data"]["0"]
    assert stripped["data"]["0"]["shell/cached"] is True  # non-body field kept

    passthrough = json.loads(_finalize_snapshot(raw, None, strip=False))
    assert passthrough["data"]["0"]["shell/outputs"] == "big"  # remote proxy verbatim


def test_body_fields_payload_returns_only_body_fields_for_the_step():
    data = {
        "0": {"type": "shell", "shell/commands": ["ls"], "shell/outputs": "OUT"},
        "1": {"type": "llm", "llm/output_text": "TEXT", "input_tokens": 2},
    }
    body0 = json.loads(_body_fields_payload(data, 0))
    assert body0 == {"step": "0", "fields": {"shell/outputs": "OUT"}}
    body1 = json.loads(_body_fields_payload(data, "1"))
    assert body1 == {"step": "1", "fields": {"llm/output_text": "TEXT"}}
    # Unknown step → None (the endpoint replies 404).
    assert _body_fields_payload(data, 99) is None


def test_live_snapshot_strips_bodies_and_body_endpoint_serves_them():
    d = _bare_drain()
    d.begin_stage(run_name="t")
    d.emit({"type": "shell", "shell/commands": ["ls"], "shell/outputs": "X" * 4000}, 0)

    snap = json.loads(d._snapshot())
    assert "shell/outputs" not in snap["data"]["0"]  # stripped from the feed
    assert "shell/commands" in snap["data"]["0"]  # summary field retained

    body = json.loads(d._body_fields("0"))
    assert body["fields"]["shell/outputs"] == "X" * 4000  # full text on demand
    assert d._body_fields("99") is None  # unknown step → 404
    assert d._body_fields("nan") is None  # unparseable step → 404


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
