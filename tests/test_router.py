"""Phase-1 router unit tests: pipeline decisions, policy, registry, normalize, result.

No engine workers exist yet, so every routable query still falls back — but the
*reasons* and the supporting machinery (normalization, guards, dirty-tracking,
quarantine, the typed result container) are all exercised here.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from synnodb.duckdb_compat.result import SynnoResult
from synnodb.router import (
    ColumnSpec,
    EngineBinding,
    PlaceholderSpec,
    QueryRouter,
    RouterMode,
    RouterPolicy,
    TemplateRegistry,
)
from synnodb.router.guards import GuardContext, evaluate
from synnodb.router.normalize import (
    extract_literals,
    is_select,
    normalize_sql,
    statement_kind,
    tables_in,
)


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
def test_policy_defaults_are_inert_without_engines():
    # Routing is live by default, but with no engines registered every query still falls
    # back (short-circuited before parsing), so behavior is byte-identical to DuckDB.
    p = RouterPolicy()
    assert p.mode is RouterMode.SAMPLED
    assert p.routing_active is True
    assert p.block_writes is True
    dec = QueryRouter(p).route("SELECT a FROM t WHERE a = 1", None, conn=None)
    assert dec.routed is False
    assert dec.trace.reason == "no engines registered"


def test_policy_from_env(monkeypatch):
    monkeypatch.setenv("SYNNODB_ROUTER", "off")
    assert RouterPolicy.from_env().enabled is False
    monkeypatch.setenv("SYNNODB_ROUTER", "sampled")
    monkeypatch.setenv("SYNNODB_CROSS_CHECK", "0.25")
    p = RouterPolicy.from_env()
    assert p.mode is RouterMode.SAMPLED and p.routing_active is True
    assert p.cross_check_rate == 0.25


def test_policy_with_override():
    p = RouterPolicy().with_(mode="sampled", cross_check_rate=0.5)
    assert p.mode is RouterMode.SAMPLED and p.cross_check_rate == 0.5


# --------------------------------------------------------------------------- #
# Normalization & classification
# --------------------------------------------------------------------------- #
def test_normalize_collapses_literals():
    a = normalize_sql("SELECT a FROM t WHERE a = 1 AND b = 'x'")
    b = normalize_sql("SELECT a FROM t WHERE a = 999 AND b = 'zzz'")
    assert a is not None and a == b


def test_normalize_distinguishes_structure():
    assert normalize_sql("SELECT a FROM t") != normalize_sql("SELECT b FROM t")


def test_normalize_returns_none_on_garbage():
    assert normalize_sql("this is not sql !!!") is None


@pytest.mark.parametrize(
    "sql,kind",
    [
        ("SELECT 1", "read"),
        ("WITH x AS (SELECT 1) SELECT * FROM x", "read"),
        ("INSERT INTO t VALUES (1)", "write"),
        ("UPDATE t SET a = 1", "write"),
        ("DELETE FROM t", "write"),
        ("CREATE TABLE t(a int)", "write"),
        ("DROP TABLE t", "write"),
        ("COPY t FROM 'f.csv'", "write"),
    ],
)
def test_statement_kind(sql, kind):
    assert statement_kind(sql) == kind


def test_is_select():
    assert is_select("SELECT 1")
    assert is_select("WITH x AS (SELECT 1) SELECT * FROM x")
    assert not is_select("INSERT INTO t VALUES (1)")


def test_extract_literals_in_order():
    assert extract_literals("SELECT a FROM t WHERE a = 5 AND b = 'x'") == [5, "x"]


def test_tables_in():
    assert set(tables_in("SELECT * FROM lineitem JOIN orders ON x")) == {
        "lineitem",
        "orders",
    }


# --------------------------------------------------------------------------- #
# Registry: register / match / quarantine / dirty
# --------------------------------------------------------------------------- #
def _binding(tables=("t",), engine=None):
    norm = normalize_sql("SELECT a FROM t WHERE a = 1")
    return EngineBinding(
        template_id="eng1::1",
        normalized_sql=norm,
        query_id="1",
        engine_id="eng1",
        placeholders=(PlaceholderSpec("p0", "INTEGER"),),
        output_schema=(ColumnSpec("a", "INTEGER"),),
        tables=frozenset(tables),
        schema_fingerprint="fp",
        engine=engine,
    )


def test_registry_match_and_quarantine():
    reg = TemplateRegistry()
    b = _binding()
    reg.register(b)
    assert reg.match(b.normalized_sql) is b
    reg.quarantine(b.template_id)
    assert reg.match(b.normalized_sql) is None
    reg.reset_quarantine(b.template_id)
    assert reg.match(b.normalized_sql) is b


def test_registry_dirty_tracking():
    reg = TemplateRegistry()
    b = _binding(tables=("t",))
    reg.register(b)
    assert reg.is_dirty(b) is False
    reg.mark_tables_dirty(["T"])  # case-insensitive
    assert reg.is_dirty(b) is True
    reg.clear_dirty(["t"])
    assert reg.is_dirty(b) is False


# --------------------------------------------------------------------------- #
# Router pipeline (fallback-always; no engines yet)
# --------------------------------------------------------------------------- #
def test_router_off_falls_back():
    r = QueryRouter(RouterPolicy(mode=RouterMode.OFF))
    dec = r.route("SELECT a FROM t WHERE a = 1", None, conn=None)
    assert dec.routed is False
    assert dec.trace.decision == "fallback"
    assert dec.trace.reason == "mode=off"


def test_router_empty_registry_short_circuits():
    # With nothing registered, route falls back without even normalizing the SQL.
    r = QueryRouter(RouterPolicy(mode=RouterMode.SAMPLED))
    dec = r.route("SELECT a FROM t WHERE a = 1", None, conn=None)
    assert dec.routed is False
    assert dec.trace.reason == "no engines registered"


def test_router_no_template_match_falls_back():
    # A registered (non-empty) registry, but the incoming query matches no template.
    reg = TemplateRegistry()
    reg.register(_binding())
    r = QueryRouter(RouterPolicy(mode=RouterMode.SAMPLED), reg)
    dec = r.route("SELECT x FROM other_table", None, conn=None)
    assert dec.routed is False
    assert dec.trace.reason == "no template match"


def test_router_matched_query_falls_back_without_engine():
    reg = TemplateRegistry()
    b = _binding(engine=None)  # registered but no live worker
    reg.register(b)
    r = QueryRouter(RouterPolicy(mode=RouterMode.SAMPLED), reg)
    dec = r.route("SELECT a FROM t WHERE a = 7", None, conn=None)
    assert dec.routed is False
    # the engine_ready guard is what forced the fallback
    guard_names = [g[0] for g in dec.trace.guard_results]
    assert "engine_ready_guard" in guard_names
    assert dec.trace.guard_results[-1][1] is False


def test_router_unparseable_falls_back():
    reg = TemplateRegistry()
    reg.register(_binding())  # non-empty, so route reaches the normalize step
    r = QueryRouter(RouterPolicy(mode=RouterMode.SAMPLED), reg)
    dec = r.route("?!? not sql", None, conn=None)
    assert dec.routed is False
    assert dec.trace.reason == "unparseable SQL"


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
def test_guards_stop_at_first_failure():
    b = _binding(engine=None)
    ctx = GuardContext(
        sql="SELECT a FROM t WHERE a = 1",
        binding=b,
        conn=None,
        registry=TemplateRegistry(),
    )
    ok, results = evaluate(ctx)
    assert ok is False
    # engine_ready is first and fails, so only one result recorded.
    assert results == [("engine_ready_guard", False, "no live engine worker bound")]


def _grouped_binding():
    """A Q13-shaped binding: one SQL `?` carrying two specs packed in a single literal."""
    template = "SELECT count(*) AS c FROM t WHERE b LIKE ?"
    return EngineBinding(
        template_id="eng13::13",
        normalized_sql=normalize_sql(template),
        query_id="13",
        engine_id="eng13",
        placeholders=(
            PlaceholderSpec("W1", "VARCHAR", "%", "%", 0),
            PlaceholderSpec("W2", "VARCHAR", "%", "%", 0),
        ),
        output_schema=(ColumnSpec("c", "BIGINT"),),
        tables=frozenset({"t"}),
        schema_fingerprint="fp",
        template_sql=template,
    )


def test_arity_guard_counts_binding_groups_not_specs():
    # A caller supplies one value per SQL `?`. Two specs packed in one literal are a single
    # binding group, so one bound value must pass and two must fail.
    from synnodb.router.guards import placeholder_arity_guard

    b = _grouped_binding()
    sql = "SELECT count(*) AS c FROM t WHERE b LIKE ?"
    ok, detail = placeholder_arity_guard(
        GuardContext(sql=sql, binding=b, conn=None, registry=None, parameters=["%x%y%"])
    )
    assert ok, detail
    ok, _ = placeholder_arity_guard(
        GuardContext(
            sql=sql, binding=b, conn=None, registry=None, parameters=["%x%", "%y%"]
        )
    )
    assert not ok


def test_arity_guard_refuses_named_parameters():
    # Router binding is positional; a dict of named parameters cannot be lined up with the
    # specs, so the guard must fall back rather than let the dict bind as a value.
    from synnodb.router.guards import placeholder_arity_guard

    b = _binding()
    ok, detail = placeholder_arity_guard(
        GuardContext(
            sql="SELECT a FROM t WHERE a = $p",
            binding=b,
            conn=None,
            registry=None,
            parameters={"p": 1},
        )
    )
    assert not ok
    assert "named" in detail


def test_literalize_types_each_marker_by_its_binding_group():
    # `_literalize` substitutes a typed NULL per `?` to describe the template's output schema.
    # The i-th `?` is the i-th binding group: after Q13's packed literal (two VARCHAR specs,
    # one `?`), a following INTEGER parameter must get its own type, not the group's second spec.
    from synnodb.router.registration import _literalize

    specs = [
        PlaceholderSpec("W1", "VARCHAR", "%", "%", 0),
        PlaceholderSpec("W2", "VARCHAR", "%", "%", 0),
        PlaceholderSpec("P", "INTEGER"),
    ]
    sql = _literalize("SELECT count(*) AS c FROM t WHERE b LIKE ? AND a >= ?", specs)
    assert sql == (
        "SELECT COUNT(*) AS c FROM t "
        "WHERE b LIKE CAST(NULL AS TEXT) AND a >= CAST(NULL AS INT)"
    )


# --------------------------------------------------------------------------- #
# SynnoResult: DuckDB-compatible cursor semantics over an Arrow table
# --------------------------------------------------------------------------- #
def _table():
    return pa.table({"a": [1, 2, 3], "b": ["x", "y", "z"]})


def test_synnoresult_fetch_semantics():
    res = SynnoResult(_table())
    assert res.fetchone() == (1, "x")
    assert res.fetchmany(1) == [(2, "y")]
    assert res.fetchall() == [(3, "z")]
    assert res.fetchone() is None


def test_synnoresult_bulk_egress_independent_of_cursor():
    res = SynnoResult(_table())
    res.fetchone()  # advance cursor
    assert res.to_arrow_table().num_rows == 3
    assert res.arrow().read_all().num_rows == 3  # RecordBatchReader, like DuckDB
    assert list(res.df()["a"]) == [1, 2, 3]
    assert res.fetchnumpy()["a"].tolist() == [1, 2, 3]


def test_synnoresult_description_uses_duckdb_types_when_given():
    res = SynnoResult(_table(), duckdb_types=["BIGINT", "VARCHAR"])
    assert [(d[0], d[1]) for d in res.description] == [
        ("a", "BIGINT"),
        ("b", "VARCHAR"),
    ]
