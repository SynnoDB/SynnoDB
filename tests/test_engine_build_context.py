"""An engine may only serve under the session context it was built and validated under.

The schema-match guard compares the live fingerprint against the one taken when the engine
was REGISTERED. That catches settings changed mid-session, but never an initial mismatch:
if the connection has already selected a different time zone (or calendar, null order,
database, search_path) than generation used, registration adopts that as the baseline and
the guard sees nothing wrong ever after. A zone-sensitive kernel then answers a different
question than the one it was proven correct on, and cross-checking is sampled, so it is not
a backstop. The engine therefore records its build context in the manifest, and
registration compares rather than adopts.
"""

from __future__ import annotations

import json

import duckdb
import pytest

from synnodb.errors import SynnoError
from synnodb.router.manifest import (
    SCHEMA_VERSION,
    SESSION_KEYS,
    EngineManifest,
    QueryTemplate,
    check_compatibility,
    read_session_context,
    session_context_problems,
)


def _ctx(**overrides) -> dict:
    base = {
        "database": "memory",
        "search_path": "",
        "null_order": "NULLS_LAST",
        "timezone": "Etc/UTC",
        "calendar": "gregorian",
    }
    base.update(overrides)
    return base


def test_read_session_context_reads_every_setting_that_changes_meaning():
    con = duckdb.connect()
    try:
        ctx = read_session_context(con)
    finally:
        con.close()
    assert set(ctx) == set(SESSION_KEYS)
    assert all(isinstance(v, str) for v in ctx.values())


def test_unreadable_context_is_unknown_not_a_match():
    class Broken:
        def execute(self, *_a, **_k):
            raise RuntimeError("no such setting")

    assert read_session_context(Broken()) == {}
    # An unknown context must never silently compare equal to a real one.
    assert session_context_problems({}, _ctx()) != []


@pytest.mark.parametrize("key", sorted(_ctx()))
def test_each_setting_is_compared(key):
    problems = session_context_problems(_ctx(**{key: "other"}), _ctx())
    assert len(problems) == 1
    assert key in problems[0]


def test_matching_context_is_no_problem():
    assert session_context_problems(_ctx(), _ctx()) == []


def test_manifest_round_trips_the_build_context():
    ctx = _ctx(timezone="Europe/Berlin")
    m = EngineManifest(
        engine_id="eng-tz",
        queries=(QueryTemplate("1", "SELECT 1"),),
        session_context=ctx,
    )
    d = json.loads(m.to_json())
    assert d["schema_version"] == SCHEMA_VERSION
    assert d["session_context"] == ctx
    assert EngineManifest.from_dict(d).session_context == ctx


def test_older_manifest_without_a_context_reads_as_unknown():
    old = {
        "schema_version": 5,
        "engine_id": "eng-old",
        "queries": [{"query_id": "1", "sql_template": "SELECT 1"}],
    }
    assert EngineManifest.from_dict(old).session_context == {}


def test_registration_gate_rejects_a_connection_in_another_zone():
    """The reproduction: the engine was built under Berlin, the connection is in New York."""
    con = duckdb.connect()
    try:
        con.execute("SET TimeZone='America/New_York'")
        live = read_session_context(con)
        manifest = EngineManifest(
            engine_id="eng-berlin",
            queries=(QueryTemplate("1", "SELECT 1"),),
            session_context={**live, "timezone": "Europe/Berlin"},
        )
        problems = check_compatibility(con, manifest)
        assert any("timezone" in p for p in problems), problems
        assert "Europe/Berlin" in problems[0] and "America/New_York" in problems[0]
    finally:
        con.close()


def test_registration_gate_accepts_the_zone_it_was_built_in():
    con = duckdb.connect()
    try:
        manifest = EngineManifest(
            engine_id="eng-same",
            queries=(QueryTemplate("1", "SELECT 1"),),
            session_context=read_session_context(con),
        )
        assert check_compatibility(con, manifest) == []
    finally:
        con.close()


def test_zone_bearing_output_is_refused_when_the_build_context_is_unknown():
    """Fail closed: with no recorded baseline there is nothing to compare, so a
    zone-bearing result cannot be shown to have been built under this zone."""
    import synnodb
    from synnodb.router import LocalCallableEngine
    from synnodb.router.manifest import register_manifest

    con = synnodb.connect(":memory:")
    try:
        con.duckdb.execute(
            "CREATE TABLE t AS SELECT TIMESTAMPTZ '2024-01-01 00:00:00' AS ts"
        )
        manifest = EngineManifest(
            engine_id="eng-nocontext",
            queries=(QueryTemplate("1", "SELECT ts FROM t"),),
            session_context={},  # an engine that never recorded one
        )
        with pytest.raises(SynnoError, match="records no build-time session context"):
            register_manifest(
                con,
                manifest,
                LocalCallableEngine("eng-nocontext", {"1": lambda ph: None}),
            )
    finally:
        con.close()
