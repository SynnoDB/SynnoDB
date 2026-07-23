"""Referential downscaler behaviour on real dataset shapes: TPC-H and CEB/IMDB.

``test_duckdb_downscale.py`` proves the algorithm on a hand-built star. This suite exercises the
same guarantees against the two schemas the downscaler is actually meant for:

* **TPC-H** - generated with the real ``dbgen`` extension and driven by the real
  ``tutorials/workloads/tpch/tpch_queries.json`` join graph. A classic single-fact star.
* **CEB / IMDB** - the production ``imdb_schema`` DDL (21 tables, declared PKs, a reserved-word
  ``name`` table) populated with FK-consistent synthetic rows, driven by the real CEB query
  templates. A snowflake whose largest table is a *bridge* (``cast_info``), so the anchor is not
  the hub every dimension hangs off - a stronger test of parent-ward propagation.

For each dataset we assert the four properties that make a downscaled subset a valid cheap
correctness rung (§9 of the design):

1. the anchor is the largest table and is strictly downscaled,
2. small / disconnected tables are kept whole,
3. no dangling join keys along the traversed edges (referential closure), and
4. every workload join skeleton still returns rows (non-vacuous).
"""

from __future__ import annotations

import json

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("sqlglot")

from synnodb.utils.path_utils import repo_root
from synnodb.workloads.dataset.custom_scaler.duckdb_downscale import (
    ReferentialDownscaler,
)


# --------------------------------------------------------------------------- shared assertions
def _count(con: "duckdb.DuckDBPyConnection", sql: str) -> int:
    return con.execute(sql).fetchone()[0]


def _modes(plan) -> dict:
    """table -> its ``SubsetTable`` for the just-materialized plan."""
    return {t.table: t for t in plan.tables}


def _assert_no_dangling(
    con: "duckdb.DuckDBPyConnection",
    ds: ReferentialDownscaler,
    child: str,
    fk: str,
    parent: str,
    pk: str,
) -> None:
    """Every kept ``child.fk`` must resolve to a kept ``parent.pk`` - the closure guarantee.

    Reads the ephemeral ``keep_*`` temp tables directly, so it must run while a subset is
    materialized. Table names are used unprefixed: a reserved word like ``name`` is safe once
    carried by the ``_synno_keep_`` prefix.
    """
    k = ds.KEEP_PREFIX
    dangling = _count(
        con,
        f"SELECT COUNT(*) FROM {k}{child} WHERE {fk} NOT IN (SELECT {pk} FROM {k}{parent})",
    )
    assert dangling == 0, (
        f"{child}.{fk} has {dangling} rows dangling out of {parent}.{pk}"
    )


def _assert_anchor_and_modes(
    ds: ReferentialDownscaler,
    plan,
    *,
    anchor: str,
    whole: set[str],
    sampled: set[str],
) -> None:
    modes = _modes(plan)
    counts = ds.schema.row_counts

    assert ds.anchor() == anchor
    a = modes[anchor]
    assert a.mode == "anchor"
    assert 0 < a.kept_rows < counts[anchor], "anchor must be strictly downscaled"

    for t in whole:
        assert modes[t].mode == "whole", f"{t} should be kept whole"
        assert modes[t].kept_rows == counts[t]

    for t in sampled:
        assert modes[t].mode == "sample", f"{t} should be sampled"
        assert 0 < modes[t].kept_rows < counts[t], f"{t} should be strictly downscaled"


# =============================================================================== TPC-H (dbgen)
_TPCH_SF = 0.1
_TPCH_FRACTION = 0.1
# supplier(1000)/nation(25)/region(5) are <= this; every other table is sampled or the anchor.
_TPCH_WHOLE_THRESHOLD = 10_000


@pytest.fixture(scope="module")
def tpch():
    """A dbgen TPC-H database + a downscaler wired to the real workload queries.

    Skips cleanly where the tpch extension or the bundled tpch_queries.json is unavailable.
    """
    queries_path = (
        repo_root() / "tutorials" / "workloads" / "tpch" / "tpch_queries.json"
    )
    if not queries_path.exists():
        pytest.skip("tutorials/workloads/tpch/tpch_queries.json not present")
    raw = json.loads(queries_path.read_text())
    sql_by_id = {k: (v["sql"] if isinstance(v, dict) else v) for k, v in raw.items()}

    con = duckdb.connect()
    try:
        con.execute(f"INSTALL tpch; LOAD tpch; CALL dbgen(sf={_TPCH_SF});")
    except Exception as e:  # pragma: no cover - environment without the tpch extension
        con.close()
        pytest.skip(f"tpch extension unavailable: {e}")

    ds = ReferentialDownscaler(
        con, sql_by_id=sql_by_id, whole_table_threshold=_TPCH_WHOLE_THRESHOLD
    )
    yield con, ds
    ds.drop()
    con.close()


def test_tpch_anchor_and_kept_whole(tpch):
    con, ds = tpch
    plan = ds.materialize_temp_subset(_TPCH_FRACTION)
    _assert_anchor_and_modes(
        ds,
        plan,
        anchor="lineitem",
        whole={"supplier", "nation", "region"},  # below the row threshold
        sampled={"orders", "customer", "part", "partsupp"},
    )
    # the anchor is sampled to (near) the requested fraction, not merely "smaller"
    assert (
        abs(
            _modes(plan)["lineitem"].kept_rows / ds.schema.row_counts["lineitem"]
            - _TPCH_FRACTION
        )
        < 0.02
    )


def test_tpch_referential_closure(tpch):
    con, ds = tpch
    ds.materialize_temp_subset(_TPCH_FRACTION)
    _assert_no_dangling(con, ds, "lineitem", "l_orderkey", "orders", "o_orderkey")
    _assert_no_dangling(con, ds, "orders", "o_custkey", "customer", "c_custkey")
    _assert_no_dangling(con, ds, "lineitem", "l_partkey", "part", "p_partkey")
    # closure holds into whole dimensions too
    _assert_no_dangling(con, ds, "lineitem", "l_suppkey", "supplier", "s_suppkey")
    _assert_no_dangling(con, ds, "customer", "c_nationkey", "nation", "n_nationkey")


def test_tpch_workload_joins_non_vacuous(tpch):
    con, ds = tpch
    ds.materialize_temp_subset(_TPCH_FRACTION)
    k = ds.KEEP_PREFIX
    # the full TPC-H star (fact + both dimension arms + the composite partsupp bridge)
    rows = _count(
        con,
        f"SELECT COUNT(*) FROM {k}lineitem l "
        f"JOIN {k}orders o ON l.l_orderkey = o.o_orderkey "
        f"JOIN {k}customer c ON o.o_custkey = c.c_custkey "
        f"JOIN {k}nation n ON c.c_nationkey = n.n_nationkey "
        f"JOIN {k}part p ON l.l_partkey = p.p_partkey "
        f"JOIN {k}supplier s ON l.l_suppkey = s.s_suppkey "
        f"JOIN {k}partsupp ps ON ps.ps_partkey = l.l_partkey "
        f"AND ps.ps_suppkey = l.l_suppkey",
    )
    assert rows > 0


def test_tpch_determinism(tpch):
    con, ds = tpch
    first = {
        t.table: t.kept_rows for t in ds.materialize_temp_subset(_TPCH_FRACTION).tables
    }
    second = {
        t.table: t.kept_rows for t in ds.materialize_temp_subset(_TPCH_FRACTION).tables
    }
    assert first == second


def test_tpch_duckdb_native_sink_is_closed(tpch, tmp_path):
    """The DuckDB-native sink (the production default) must carry the same closure the temp
    subset has: joins in the standalone ``subset.duckdb`` are non-vacuous with no dangling keys.
    """
    con, ds = tpch
    out = tmp_path / "fraction0.1" / "subset.duckdb"
    ds.copy_subset_to_duckdb(_TPCH_FRACTION, out)

    sub = duckdb.connect(str(out), read_only=True)
    try:
        assert (
            _count(
                sub,
                "SELECT COUNT(*) FROM lineitem l "
                "WHERE l.l_orderkey NOT IN (SELECT o_orderkey FROM orders)",
            )
            == 0
        )
        joined = _count(
            sub,
            "SELECT COUNT(*) FROM lineitem l "
            "JOIN orders o ON l.l_orderkey = o.o_orderkey "
            "JOIN customer c ON o.o_custkey = c.c_custkey",
        )
        assert joined > 0
    finally:
        sub.close()


def test_tpch_full_subset_keeps_everything(tpch):
    con, ds = tpch
    plan = ds.plan_subset(1.0)
    assert plan.tables and all(t.mode == "whole" for t in plan.tables)


# ============================================================================= CEB / IMDB
_IMDB_FRACTION = 0.25
# keyword(500)/company_name(800) and the lookup dims sit below this; the fact/bridge tables
# above it (title, name, movie_info, movie_keyword) are sampled - except ones the CEB graph
# leaves disconnected from the anchor, which are kept whole regardless of size.
_IMDB_WHOLE_THRESHOLD = 1_000

# Table sizes chosen so cast_info is strictly the largest (the anchor is a bridge table) and the
# populated fact/bridge tables clear the whole-table threshold. FKs are ``i % parent_size`` so
# every child row references a real parent - the referential invariant the downscaler preserves.
_IMDB_SIZES = {
    "kind_type": 7,
    "role_type": 11,
    "info_type": 100,
    "company_type": 4,
    "keyword": 500,
    "company_name": 800,
    "name": 1500,
    "title": 2000,
    "movie_companies": 2500,
    "movie_keyword": 3000,
    "movie_info": 4000,
    "cast_info": 8000,
}


def _populate_imdb(con: "duckdb.DuckDBPyConnection") -> None:
    from tutorials.workloads.ceb.imdb_schema import imdb_schema

    con.execute(imdb_schema)
    n = _IMDB_SIZES
    # lookup / dimension tables (small -> kept whole)
    con.execute(
        f"INSERT INTO kind_type SELECT i, 'k' || i FROM range({n['kind_type']}) t(i)"
    )
    con.execute(
        f"INSERT INTO role_type SELECT i, 'r' || i FROM range({n['role_type']}) t(i)"
    )
    con.execute(
        f"INSERT INTO info_type SELECT i, 'info' || i FROM range({n['info_type']}) t(i)"
    )
    con.execute(
        f"INSERT INTO company_type SELECT i, 'ct' || i FROM range({n['company_type']}) t(i)"
    )
    con.execute(
        f"INSERT INTO keyword SELECT i, 'kw' || i, NULL FROM range({n['keyword']}) t(i)"
    )
    con.execute(
        "INSERT INTO company_name SELECT i, 'co' || i, 'US', NULL, NULL, NULL, NULL "
        f"FROM range({n['company_name']}) t(i)"
    )
    # person dimension - large enough to be sampled
    con.execute(
        "INSERT INTO \"name\" SELECT i, 'p' || i, NULL, NULL, "
        "CASE WHEN i % 2 = 0 THEN 'm' ELSE 'f' END, NULL, NULL, NULL, NULL "
        f"FROM range({n['name']}) t(i)"
    )
    # title (movies) - the schema hub
    con.execute(
        "INSERT INTO title SELECT i, 't' || i, NULL, (i % 7), 1990 + (i % 30), "
        f"NULL, NULL, NULL, NULL, NULL, NULL, NULL FROM range({n['title']}) t(i)"
    )
    # fact / bridge tables (movie_id -> title, person_id -> name, *_type_id -> the dims)
    con.execute(
        "INSERT INTO cast_info SELECT i, (i % 1500), (i % 2000), NULL, NULL, NULL, (i % 11) "
        f"FROM range({n['cast_info']}) t(i)"
    )
    con.execute(
        "INSERT INTO movie_info SELECT i, (i % 2000), (i % 100), 'x', NULL "
        f"FROM range({n['movie_info']}) t(i)"
    )
    con.execute(
        "INSERT INTO movie_keyword SELECT i, (i % 2000), (i % 500) "
        f"FROM range({n['movie_keyword']}) t(i)"
    )
    con.execute(
        "INSERT INTO movie_companies SELECT i, (i % 2000), (i % 800), (i % 4), NULL "
        f"FROM range({n['movie_companies']}) t(i)"
    )


@pytest.fixture(scope="module")
def imdb():
    """A synthetic IMDB database (real DDL + FK-consistent rows) and a downscaler driven by two
    representative CEB templates (Q1a: title/cast/info; Q2a: adds the keyword bridge)."""
    from tutorials.workloads.ceb.ceb_queries import ceb_templates

    con = duckdb.connect()
    _populate_imdb(con)
    sql_by_id = {qid: ceb_templates[qid] for qid in ("Q1a", "Q2a")}
    ds = ReferentialDownscaler(
        con, sql_by_id=sql_by_id, whole_table_threshold=_IMDB_WHOLE_THRESHOLD
    )
    yield con, ds
    ds.drop()
    con.close()


def test_imdb_anchor_is_bridge_table_and_modes(imdb):
    con, ds = imdb
    plan = ds.materialize_temp_subset(_IMDB_FRACTION)
    _assert_anchor_and_modes(
        ds,
        plan,
        anchor="cast_info",  # the largest table is a bridge, not the hub
        whole={
            "keyword",  # below threshold
            "kind_type",
            "role_type",
            "info_type",  # lookup dims
            "movie_companies",  # above threshold but disconnected from the anchor's join graph
            "company_name",
        },
        sampled={"title", "name", "movie_info", "movie_keyword"},
    )


def test_imdb_referential_closure(imdb):
    con, ds = imdb
    ds.materialize_temp_subset(_IMDB_FRACTION)
    # every bridge row resolves both of its ways out (movie side and person side)
    _assert_no_dangling(con, ds, "cast_info", "movie_id", "title", "id")
    _assert_no_dangling(con, ds, "cast_info", "person_id", "name", "id")
    # movie_* tables propagated parent-ward from title resolve into the kept titles
    _assert_no_dangling(con, ds, "movie_info", "movie_id", "title", "id")
    _assert_no_dangling(con, ds, "movie_keyword", "movie_id", "title", "id")
    # and into the whole keyword dimension
    _assert_no_dangling(con, ds, "movie_keyword", "keyword_id", "keyword", "id")


def test_imdb_workload_joins_non_vacuous(imdb):
    con, ds = imdb
    ds.materialize_temp_subset(_IMDB_FRACTION)
    k = ds.KEEP_PREFIX
    # the Q2a join skeleton: title fact joined to the cast arm, the movie-info arm and the
    # keyword arm, each through its lookup dimension
    rows = _count(
        con,
        f"SELECT COUNT(*) FROM {k}title t "
        f"JOIN {k}cast_info ci ON t.id = ci.movie_id "
        f"JOIN {k}name nm ON ci.person_id = nm.id "
        f"JOIN {k}role_type rt ON ci.role_id = rt.id "
        f"JOIN {k}kind_type kt ON t.kind_id = kt.id "
        f"JOIN {k}movie_info mi ON t.id = mi.movie_id "
        f"JOIN {k}info_type it ON mi.info_type_id = it.id "
        f"JOIN {k}movie_keyword mk ON t.id = mk.movie_id "
        f"JOIN {k}keyword kw ON mk.keyword_id = kw.id",
    )
    assert rows > 0


def test_imdb_determinism(imdb):
    con, ds = imdb
    first = {
        t.table: t.kept_rows for t in ds.materialize_temp_subset(_IMDB_FRACTION).tables
    }
    second = {
        t.table: t.kept_rows for t in ds.materialize_temp_subset(_IMDB_FRACTION).tables
    }
    assert first == second


def test_imdb_parquet_sink_is_closed(imdb, tmp_path):
    """The parquet fallback sink must preserve closure just like the temp subset: the kept
    cast_info still joins to the kept title through the materialized parquet files."""
    out = tmp_path / "fraction0.25"
    ds = imdb[1]
    ds.copy_subset_to_parquet(_IMDB_FRACTION, out)

    rc = duckdb.connect()
    try:
        for table in ("title", "cast_info"):
            rc.execute(
                f'CREATE VIEW "{table}" AS '
                f"SELECT * FROM read_parquet('{(out / f'{table}.parquet').as_posix()}')"
            )
        assert (
            _count(
                rc,
                "SELECT COUNT(*) FROM cast_info ci "
                "WHERE ci.movie_id NOT IN (SELECT id FROM title)",
            )
            == 0
        )
        joined = _count(
            rc,
            "SELECT COUNT(*) FROM title t JOIN cast_info ci ON t.id = ci.movie_id",
        )
        assert joined > 0
    finally:
        rc.close()
