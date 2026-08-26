"""Serving queries whose output carries a time zone.

A DuckDB ``TIMESTAMP WITH TIME ZONE`` is a UTC instant plus a session-level display zone.
The engine emits the instant (the same int64 microseconds DuckDB stores) and the router
labels it with the session's zone, so the served Arrow type and the Python datetimes a
caller fetches are indistinguishable from DuckDB's own.

The zone is not cosmetic: a query over a zone-bearing column computes *different* answers
under different session zones - ``date_trunc('day', ...)`` can land on a different day - so
an engine is only valid under the zone it was built for. The schema fingerprint pins that,
and the last test here proves a zone change stops the engine serving.

Run: .venv/bin/python -m pytest tests/test_timezone_egress.py -q
"""

from __future__ import annotations

import duckdb
import pyarrow as pa
import pytest

import synnodb
from synnodb.router import (
    LocalCallableEngine,
    RouterMode,
    RouterPolicy,
    TemplateRegistry,
    register_engine,
)
from synnodb.router.registration import describe_output

# One UTC instant: 2024-06-01T12:00Z. Berlin renders it 14:00+02, New York 08:00-04.
INSTANT_US = 1_717_243_200_000_000
TEMPLATE = "SELECT ts FROM events ORDER BY ts"


def _con(zone="Europe/Berlin"):
    con = synnodb.connect(
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
        registry=TemplateRegistry(),
    )
    con.duckdb.execute(f"SET TimeZone='{zone}'")
    con.duckdb.execute("CREATE TABLE events(ts TIMESTAMPTZ)")
    con.duckdb.execute(
        "INSERT INTO events VALUES (TIMESTAMPTZ '2024-06-01 12:00:00+00')"
    )
    return con


def _engine_table():
    """What a bespoke engine produces: the UTC instant, unlabelled."""
    return pa.table({"ts": pa.array([INSTANT_US], pa.timestamp("us"))})


def test_zone_bearing_output_is_registrable():
    con = _con()
    try:
        schema = describe_output(con, TEMPLATE)
        assert [c.type for c in schema] == ["TIMESTAMP WITH TIME ZONE"]
        register_engine(
            con,
            template_sql=TEMPLATE,
            engine=LocalCallableEngine("eng-tz", {"1": lambda ph: _engine_table()}),
            placeholders=[],
        )
        assert con.router_stats()["registry"]["templates"] == 1
    finally:
        con.close()


@pytest.mark.parametrize("zone", ["Europe/Berlin", "America/New_York", "UTC"])
def test_served_result_is_indistinguishable_from_duckdb(zone):
    """Same Arrow type, same Python datetimes, and the cross-check agrees - in any zone."""
    con = _con(zone)
    try:
        register_engine(
            con,
            template_sql=TEMPLATE,
            engine=LocalCallableEngine("eng-tz", {"1": lambda ph: _engine_table()}),
            placeholders=[],
        )
        served = con.execute(TEMPLATE).fetchall()
        assert con._last["served_by"] == "engine"

        reference = duckdb.connect()
        reference.execute(f"SET TimeZone='{zone}'")
        reference.execute("CREATE TABLE events(ts TIMESTAMPTZ)")
        reference.execute(
            "INSERT INTO events VALUES (TIMESTAMPTZ '2024-06-01 12:00:00+00')"
        )
        expected = reference.execute(TEMPLATE).to_arrow_table()
        reference.close()

        assert served == [(expected.column(0).to_pylist()[0],)]
        assert served[0][0].tzinfo is not None
        assert con.router_stats()["session"]["cross_check_mismatch"] == 0
    finally:
        con.close()


def test_engine_only_serves_the_zone_it_was_built_for():
    # date_trunc over a zone-bearing column is zone-dependent (2024-06-01 in Berlin is
    # 2024-05-31 in New York for an early-morning UTC instant), so an engine built under one
    # zone must not answer under another. The session fingerprint enforces it.
    con = _con("Europe/Berlin")
    try:
        register_engine(
            con,
            template_sql=TEMPLATE,
            engine=LocalCallableEngine("eng-tz", {"1": lambda ph: _engine_table()}),
            placeholders=[],
        )
        # Executed without fetching: DuckDB's own fetch of a zone-bearing value needs pytz,
        # which the routed path does not, and this test is about routing, not egress.
        con.execute(TEMPLATE)
        assert con._last["served_by"] == "engine"

        con.duckdb.execute("SET TimeZone='America/New_York'")
        con.execute(TEMPLATE)
        assert con._last["served_by"] == "duckdb"

        con.duckdb.execute("SET TimeZone='Europe/Berlin'")
        con.execute(TEMPLATE)
        assert con._last["served_by"] == "engine"
    finally:
        con.close()


def test_unlabelled_engine_output_would_not_match_duckdb():
    """Why the labelling step exists: the raw engine table compares unequal to DuckDB's."""
    reference = duckdb.connect()
    reference.execute("SET TimeZone='Europe/Berlin'")
    reference.execute("CREATE TABLE events(ts TIMESTAMPTZ)")
    reference.execute(
        "INSERT INTO events VALUES (TIMESTAMPTZ '2024-06-01 12:00:00+00')"
    )
    expected = reference.execute(TEMPLATE).to_arrow_table()
    reference.close()
    assert _engine_table().column(0).to_pylist() != expected.column(0).to_pylist()


def test_refuses_to_serve_when_the_session_zone_is_unreadable():
    """Fail closed: without a zone the column could only go out tz-naive under a
    zone-bearing description, which is exactly what refusing the type used to prevent."""
    con = _con()
    try:
        register_engine(
            con,
            template_sql=TEMPLATE,
            engine=LocalCallableEngine("eng-tz", {"1": lambda ph: _engine_table()}),
            placeholders=[],
        )
        con.execute(TEMPLATE)
        assert con._last["served_by"] == "engine"

        object.__setattr__(con, "session_timezone", lambda: None)
        con.execute(TEMPLATE)
        assert con._last["served_by"] == "duckdb"
        reasons = con.router_stats()["session"]["fallback_reasons"]
        assert any("time zone" in r for r in reasons)
    finally:
        con.close()


def test_naive_output_never_touches_the_connection():
    """A schema with no zone-bearing column pays nothing for this machinery."""
    from synnodb.router.adapt import stamp_timezones
    from synnodb.router.registry import ColumnSpec

    class Exploding:
        def session_timezone(self):
            raise AssertionError("must not be consulted for a naive schema")

    table = pa.table({"n": pa.array([1], pa.int64())})
    assert stamp_timezones(table, [ColumnSpec("n", "BIGINT")], Exploding()) is table


def test_align_timezones_matches_the_reference():
    """The generation-time counterpart: an engine's bare UTC microseconds compare equal to
    DuckDB's aware datetimes once labelled from the reference. Without this the whole run
    reads as a divergence and the model chases a phantom bug."""
    from synnodb.router.adapt import align_timezones, results_equal

    reference = duckdb.connect()
    reference.execute("SET TimeZone='Europe/Berlin'")
    expected = reference.execute(
        "SELECT TIMESTAMPTZ '2024-06-01 12:00:00+00' AS ts"
    ).to_arrow_table()
    reference.close()

    bespoke = _engine_table()
    assert not results_equal(bespoke, expected, ordered=False)
    assert results_equal(align_timezones(bespoke, expected), expected, ordered=False)
