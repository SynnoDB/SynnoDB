"""Zero-overhead drop-in: with no engines registered, the router falls back without parsing,
so an engine-less connection pays nothing per query.
"""
from __future__ import annotations

import synnodb
from synnodb.router import QueryRouter, RouterMode, RouterPolicy, TemplateRegistry
from synnodb.router import router as router_module


def test_empty_registry_does_not_normalize(monkeypatch):
    calls = {"n": 0}
    real = router_module.normalize_sql

    def spy(sql):
        calls["n"] += 1
        return real(sql)

    monkeypatch.setattr(router_module, "normalize_sql", spy)
    r = QueryRouter(RouterPolicy(mode=RouterMode.SAMPLED))  # empty registry
    dec = r.route("SELECT a FROM t WHERE a = 1", None, conn=None)
    assert dec.routed is False and dec.trace.reason == "no engines registered"
    assert calls["n"] == 0  # never parsed


def test_nonempty_registry_does_normalize(monkeypatch):
    from synnodb.router.normalize import normalize_sql
    from synnodb.router.registry import ColumnSpec, EngineBinding, PlaceholderSpec

    reg = TemplateRegistry()
    reg.register(EngineBinding(
        template_id="e::1", normalized_sql=normalize_sql("SELECT a FROM t WHERE a = 1"),
        query_id="1", engine_id="e", placeholders=(PlaceholderSpec("p0", "INTEGER"),),
        output_schema=(ColumnSpec("a", "INTEGER"),), tables=frozenset({"t"}),
        schema_fingerprint="fp",
    ))
    calls = {"n": 0}
    real = router_module.normalize_sql
    monkeypatch.setattr(router_module, "normalize_sql", lambda s: (calls.__setitem__("n", calls["n"] + 1), real(s))[1])
    QueryRouter(RouterPolicy(mode=RouterMode.SAMPLED), reg).route("SELECT x FROM other", None, conn=None)
    assert calls["n"] == 1  # parsed once to compute the structural key


def test_mode_off_is_inert():
    con = synnodb.connect(policy=RouterPolicy(mode=RouterMode.OFF))
    con.duckdb.execute("CREATE TABLE t(a int)")
    con.duckdb.execute("INSERT INTO t VALUES (1),(2)")
    assert con.execute("SELECT count(*) FROM t").fetchall() == [(2,)]
    assert con.router_stats()["mode"] == "off"
