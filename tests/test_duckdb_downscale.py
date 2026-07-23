"""FK-preserving (referential) downscaling of a DuckDB source.

Covers the design's test plan (§9): join-graph inference from the workload queries, the
referential-closure properties of a downscaled subset (no dangling join keys, non-vacuous joins,
determinism, small/disconnected tables kept whole), and end-to-end registration of a workload
sourced from a DuckDB connection via the parquet fallback.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from synnodb.utils.path_utils import repo_root
from synnodb.workloads.dataset.custom_scaler.duckdb_downscale import (
    JoinEdge,
    ReferentialDownscaler,
    build_join_graph,
    infer_join_edges_from_sql,
    introspect,
)


# --------------------------------------------------------------------------- synthetic schema
def _make_synthetic(con: duckdb.DuckDBPyConnection) -> None:
    """A minimal star: lineitem (fact) -> orders -> customer -> nation (dim), plus a
    disconnected ``island`` table. FKs are ``i % parent_size`` so every child row references a
    real parent - the invariant the downscaler must preserve after sampling.
    """
    con.execute(
        "CREATE TABLE nation AS SELECT i AS n_nationkey, 'n' || i AS n_name FROM range(5) t(i)"
    )
    con.execute(
        "CREATE TABLE customer AS "
        "SELECT i AS c_custkey, (i % 5) AS c_nationkey FROM range(50) t(i)"
    )
    con.execute(
        "CREATE TABLE orders AS "
        "SELECT i AS o_orderkey, (i % 50) AS o_custkey FROM range(200) t(i)"
    )
    con.execute(
        "CREATE TABLE lineitem AS "
        "SELECT i AS l_id, (i % 200) AS l_orderkey FROM range(1000) t(i)"
    )
    con.execute("CREATE TABLE island AS SELECT i AS x FROM range(3) t(i)")


_SYNTHETIC_QUERIES = {
    "1": "SELECT * FROM lineitem l JOIN orders o ON l.l_orderkey = o.o_orderkey",
    "2": "SELECT * FROM orders o JOIN customer c ON o.o_custkey = c.c_custkey",
    "3": "SELECT * FROM customer c JOIN nation n ON c.c_nationkey = n.n_nationkey",
}


@pytest.fixture
def synthetic_con():
    con = duckdb.connect()
    _make_synthetic(con)
    yield con
    con.close()


@pytest.fixture
def downscaler(synthetic_con):
    # threshold 10 -> nation(5) + island(3) kept whole; customer(50), orders(200) sampled
    return ReferentialDownscaler(
        synthetic_con, sql_by_id=_SYNTHETIC_QUERIES, whole_table_threshold=10
    )


# --------------------------------------------------------------------------- join inference
def test_infer_edges_from_single_query(synthetic_con):
    schema = introspect(synthetic_con)
    edges = infer_join_edges_from_sql(
        "SELECT * FROM lineitem l JOIN orders o ON l.l_orderkey = o.o_orderkey", schema
    )
    assert JoinEdge.make("lineitem", "l_orderkey", "orders", "o_orderkey") in edges


def test_infer_edges_resolves_unqualified_columns(synthetic_con):
    schema = introspect(synthetic_con)
    # no table qualifiers; resolved by unique column ownership
    edges = infer_join_edges_from_sql(
        "SELECT * FROM orders, customer WHERE o_custkey = c_custkey", schema
    )
    assert JoinEdge.make("orders", "o_custkey", "customer", "c_custkey") in edges


def test_infer_edges_ignores_column_literal_predicates(synthetic_con):
    schema = introspect(synthetic_con)
    edges = infer_join_edges_from_sql(
        "SELECT * FROM orders WHERE o_custkey = 5", schema
    )
    assert edges == set()


def test_build_join_graph_unions_explicit_relationships(synthetic_con):
    schema = introspect(synthetic_con)
    edges = build_join_graph(
        schema,
        sql_by_id={},
        con=synthetic_con,
        explicit_relationships=[("orders.o_custkey", "customer.c_custkey")],
    )
    assert JoinEdge.make("orders", "o_custkey", "customer", "c_custkey") in edges


def test_explicit_relationship_bad_shape_rejected(synthetic_con):
    schema = introspect(synthetic_con)
    with pytest.raises(ValueError, match="table.column"):
        build_join_graph(
            schema, explicit_relationships=[("orders", "customer.c_custkey")]
        )


def test_join_inference_recovers_tpch_edges():
    """Feed the real TPC-H tpch_queries.json; the recovered join graph must contain the known
    TPC-H relationships with no declared constraints and no explicit hints (§9)."""
    pytest.importorskip("sqlglot")
    queries_path = repo_root() / "tutorials" / "tpch_queries.json"
    if not queries_path.exists():
        pytest.skip("tutorials/tpch_queries.json not present")
    raw = json.loads(queries_path.read_text())
    sql_by_id = {k: (v["sql"] if isinstance(v, dict) else v) for k, v in raw.items()}

    con = duckdb.connect()
    try:
        try:
            con.execute("INSTALL tpch; LOAD tpch; CALL dbgen(sf=0.01);")
        except (
            Exception
        ) as e:  # pragma: no cover - environment without the tpch extension
            pytest.skip(f"tpch extension unavailable: {e}")
        schema = introspect(con)
        edges = build_join_graph(schema, sql_by_id=sql_by_id, con=con)
    finally:
        con.close()

    expected = {
        JoinEdge.make("customer", "c_custkey", "orders", "o_custkey"),
        JoinEdge.make("lineitem", "l_orderkey", "orders", "o_orderkey"),
        JoinEdge.make("lineitem", "l_partkey", "part", "p_partkey"),
        JoinEdge.make("lineitem", "l_suppkey", "supplier", "s_suppkey"),
        JoinEdge.make("lineitem", "l_partkey", "partsupp", "ps_partkey"),
        JoinEdge.make("lineitem", "l_suppkey", "partsupp", "ps_suppkey"),
        JoinEdge.make("part", "p_partkey", "partsupp", "ps_partkey"),
        JoinEdge.make("partsupp", "ps_suppkey", "supplier", "s_suppkey"),
        JoinEdge.make("customer", "c_nationkey", "nation", "n_nationkey"),
        JoinEdge.make("supplier", "s_nationkey", "nation", "n_nationkey"),
        JoinEdge.make("nation", "n_regionkey", "region", "r_regionkey"),
    }
    assert expected <= edges, f"missing edges: {expected - edges}"


# --------------------------------------------------------------------------- downscaler core
def test_anchor_is_largest_table(downscaler):
    assert downscaler.anchor() == "lineitem"


def _kept(con, downscaler, table):
    return con.execute(
        f"SELECT COUNT(*) FROM {downscaler._keep_name(table)}"
    ).fetchone()[0]


def test_no_dangling_join_keys(synthetic_con, downscaler):
    """After sampling, every kept child key must resolve to a kept parent along every
    traversed edge - the referential-closure guarantee."""
    downscaler.materialize_temp_subset(0.2)
    K = downscaler.KEEP_PREFIX
    dangling_li_ord = synthetic_con.execute(
        f"SELECT COUNT(*) FROM {K}lineitem l "
        f"WHERE l.l_orderkey NOT IN (SELECT o_orderkey FROM {K}orders)"
    ).fetchone()[0]
    dangling_ord_cust = synthetic_con.execute(
        f"SELECT COUNT(*) FROM {K}orders o "
        f"WHERE o.o_custkey NOT IN (SELECT c_custkey FROM {K}customer)"
    ).fetchone()[0]
    dangling_cust_nation = synthetic_con.execute(
        f"SELECT COUNT(*) FROM {K}customer c "
        f"WHERE c.c_nationkey NOT IN (SELECT n_nationkey FROM {K}nation)"
    ).fetchone()[0]
    assert dangling_li_ord == 0
    assert dangling_ord_cust == 0
    assert dangling_cust_nation == 0
    downscaler.drop()


def test_composite_key_matched_as_tuple():
    """A composite relationship must match its column pairs as one tuple: a child row survives
    only if its whole key exists on a single kept parent row, not if each component appears
    somewhere in the kept parent. Independent per-column ``IN`` checks would keep a phantom like
    ``(partkey=1, suppkey=20)`` drawn from two different parent rows - a dangling composite FK.
    """
    con = duckdb.connect()
    try:
        # psupp (100 rows) is the anchor; li (50) is sampled against it over the composite key.
        con.execute(
            "CREATE TABLE psupp AS SELECT i AS pk, i * 10 AS sk FROM range(100) t(i)"
        )
        con.execute(
            "CREATE TABLE li AS SELECT i AS pk, i * 10 AS sk, i AS extra FROM range(50) t(i)"
        )
        queries = {
            "1": "SELECT * FROM li JOIN psupp ON li.pk = psupp.pk AND li.sk = psupp.sk"
        }
        ds = ReferentialDownscaler(con, sql_by_id=queries, whole_table_threshold=1)
        assert ds.anchor() == "psupp"

        plan = ds.plan_subset(0.5)
        li_sql = next(t.sql for t in plan.tables if t.table == "li")
        assert "EXISTS" in li_sql, (
            li_sql
        )  # tuple semantics, not two independent IN checks

        # Force a cross-pattern parent keep-set so component values span rows that share no tuple.
        ds._drop_keep_tables()
        con.execute(
            f"CREATE TEMP TABLE {ds._keep_name('psupp')} AS "
            "SELECT * FROM (VALUES (1, 10), (2, 20)) v(pk, sk)"
        )
        con.execute("DROP TABLE li")
        con.execute(
            "CREATE TABLE li AS "
            "SELECT * FROM (VALUES (1, 10, 1), (1, 20, 2), (2, 20, 3)) v(pk, sk, extra)"
        )

        kept = {(pk, sk) for pk, sk, _ in con.execute(li_sql).fetchall()}
        # (1,20) is the phantom: pk=1 from one parent row, sk=20 from another, no matching tuple.
        assert kept == {(1, 10), (2, 20)}
    finally:
        con.close()


def test_workload_joins_non_vacuous(synthetic_con, downscaler):
    """The whole point: every workload join still produces rows on the downscaled subset."""
    downscaler.materialize_temp_subset(0.2)
    K = downscaler.KEEP_PREFIX
    for sql in (
        f"SELECT COUNT(*) FROM {K}lineitem l JOIN {K}orders o ON l.l_orderkey = o.o_orderkey",
        f"SELECT COUNT(*) FROM {K}orders o JOIN {K}customer c ON o.o_custkey = c.c_custkey",
        f"SELECT COUNT(*) FROM {K}customer c JOIN {K}nation n ON c.c_nationkey = n.n_nationkey",
    ):
        assert synthetic_con.execute(sql).fetchone()[0] > 0, sql
    downscaler.drop()


def test_anchor_actually_downscaled(synthetic_con, downscaler):
    downscaler.materialize_temp_subset(0.2)
    kept = _kept(synthetic_con, downscaler, "lineitem")
    assert 0 < kept < 1000  # sampled, not whole, not empty
    downscaler.drop()


def test_small_dims_and_disconnected_kept_whole(synthetic_con, downscaler):
    plan = downscaler.materialize_temp_subset(0.2)
    modes = {t.table: t.mode for t in plan.tables}
    assert modes["nation"] == "whole"  # below threshold
    assert modes["island"] == "whole"  # disconnected from the anchor
    assert _kept(synthetic_con, downscaler, "nation") == 5
    assert _kept(synthetic_con, downscaler, "island") == 3
    assert modes["lineitem"] == "anchor"
    assert modes["orders"] == "sample"
    downscaler.drop()


def test_determinism(synthetic_con, downscaler):
    first = {
        t.table: t.kept_rows for t in downscaler.materialize_temp_subset(0.2).tables
    }
    second = {
        t.table: t.kept_rows for t in downscaler.materialize_temp_subset(0.2).tables
    }
    assert first == second
    downscaler.drop()


def test_full_subset_keeps_everything(synthetic_con, downscaler):
    plan = downscaler.plan_subset(1.0)
    assert all(t.mode == "whole" for t in plan.tables)


def test_invalid_fraction_rejected(downscaler):
    with pytest.raises(ValueError):
        downscaler.plan_subset(0.0)
    with pytest.raises(ValueError):
        downscaler.plan_subset(1.5)


def test_self_referential_edge_rejected(synthetic_con):
    with pytest.raises(ValueError, match="[Ss]elf-referential"):
        ReferentialDownscaler(
            synthetic_con,
            join_relationships=[("orders.o_orderkey", "orders.o_custkey")],
        )


def test_copy_subset_to_parquet_roundtrip(synthetic_con, downscaler, tmp_path):
    out = tmp_path / "fraction0.2"
    downscaler.copy_subset_to_parquet(0.2, out)
    for table in ("lineitem", "orders", "customer", "nation", "island"):
        assert (out / f"{table}.parquet").exists()
    # the materialized parquet must preserve referential closure just like the temp subset
    rc = duckdb.connect()
    try:
        for table in ("lineitem", "orders"):
            rc.execute(
                f"CREATE VIEW {table} AS "
                f"SELECT * FROM read_parquet('{(out / f'{table}.parquet').as_posix()}')"
            )
        joined = rc.execute(
            "SELECT COUNT(*) FROM lineitem l JOIN orders o ON l.l_orderkey = o.o_orderkey"
        ).fetchone()[0]
        assert joined > 0
    finally:
        rc.close()


# --------------------------------------------------------------------------- source snapshot
def _make_constrained_source(path: Path) -> None:
    """A small referential chain region <- nation <- supplier with declared PK/FK constraints and
    a DECIMAL column, so a snapshot's constraint- and type-fidelity can both be checked."""
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE region(r_regionkey INTEGER PRIMARY KEY, r_name VARCHAR NOT NULL)"
    )
    con.execute(
        "CREATE TABLE nation(n_nationkey INTEGER PRIMARY KEY, n_name VARCHAR, "
        "n_regionkey INTEGER REFERENCES region(r_regionkey))"
    )
    con.execute(
        "CREATE TABLE supplier(s_suppkey INTEGER PRIMARY KEY, "
        "s_nationkey INTEGER REFERENCES nation(n_nationkey), s_acctbal DECIMAL(15,2))"
    )
    con.execute("INSERT INTO region VALUES (0, 'AMER'), (1, 'EMEA')")
    con.execute("INSERT INTO nation VALUES (0, 'US', 0), (1, 'DE', 1)")
    con.execute("INSERT INTO supplier VALUES (5, 1, 100.50), (6, 0, 200.00)")
    con.close()


def test_readonly_snapshot_preserves_pk_fk_constraints(tmp_path):
    """A read-only source connection snapshots via the Arrow path (COPY FROM DATABASE cannot attach
    a writable target through it). That path must still reproduce declared PK/FK constraints - the
    join-graph signal - by reading duckdb_constraints() and rebuilding each table with its
    constraints inline, inserting rows in foreign-key dependency order."""
    from synnodb.workloads.dataset.custom_scaler.duckdb_downscale import (
        snapshot_source_database,
    )

    src = tmp_path / "src.duckdb"
    _make_constrained_source(src)
    snap = tmp_path / "snap.duckdb"

    ro = duckdb.connect(str(src), read_only=True)
    try:
        snapshot_source_database(ro, snap)
    finally:
        ro.close()

    chk = duckdb.connect(str(snap), read_only=True)
    try:
        kinds = {
            (t, c)
            for t, c in chk.execute(
                "SELECT table_name, constraint_type FROM duckdb_constraints() "
                "WHERE constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')"
            ).fetchall()
        }
        assert ("region", "PRIMARY KEY") in kinds
        assert ("nation", "FOREIGN KEY") in kinds
        assert ("supplier", "FOREIGN KEY") in kinds
        # data (and its exact DECIMAL type) survives the rebuild-and-reinsert
        assert chk.execute(
            "SELECT s_acctbal FROM supplier WHERE s_suppkey = 5"
        ).fetchone()[0] == Decimal("100.50")
    finally:
        chk.close()

    # the reproduced foreign key is actually enforced, not just declared
    rw = duckdb.connect(str(snap))
    try:
        with pytest.raises(duckdb.ConstraintException):
            rw.execute("INSERT INTO nation VALUES (9, 'XX', 999)")
    finally:
        rw.close()


# --------------------------------------------------------------------------- registration
def test_register_workload_from_duckdb_end_to_end(tmp_path):
    """A workload registered straight from a DuckDB connection snapshots the full data eagerly and
    downscales the fractional rung lazily at run start (the provider's ``prepare``), yielding a
    subset whose joins are non-vacuous (the fallback path the whole factory + oracle run against)."""
    from synnodb.utils.utils import DBStorage
    from synnodb.workloads.byo_workload import register_workload_from_duckdb
    from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider
    from synnodb.workloads.workload_spec import find_sf_dir

    con = duckdb.connect()
    _make_synthetic(con)
    managed = tmp_path / "subsets"
    spec = register_workload_from_duckdb(
        name="synthetic_byo",
        con=con,
        queries_json=_SYNTHETIC_QUERIES,
        managed_root=managed,
        downscale_fractions=(0.2,),
        whole_table_threshold=10,
        serve_from="parquet",  # this test exercises the parquet fallback specifically
    )
    con.close()

    assert spec.benchmark_sf == 1.0
    assert spec.fast_check_sfs == (0.2,)
    assert set(spec.tables) == {"lineitem", "orders", "customer", "nation", "island"}
    assert spec.dataset_version is not None

    # Sync materializes only the full benchmark subset; the fractional rung is downscaled lazily.
    assert find_sf_dir(managed, 1.0) is not None
    assert find_sf_dir(managed, 0.2) is None

    # The provider downscales the fractional subset on demand at run start.
    prov = OLAPWorkloadProvider(
        benchmark="synthetic_byo",
        base_parquet_dir=managed,
        db_storage=DBStorage.IN_MEMORY,
        query_ids=["1"],
    )
    prov.prepare()

    fraction_dir = find_sf_dir(managed, 0.2)
    full_dir = find_sf_dir(managed, 1.0)
    assert fraction_dir is not None and fraction_dir.name == "fraction0.2"
    assert full_dir is not None and full_dir.name == "fraction1"

    # Oracle-style read of the downscaled subset: joins must be non-empty.
    oc = duckdb.connect()
    try:
        for table in ("lineitem", "orders", "customer"):
            oc.execute(
                f"CREATE VIEW {table} AS "
                f"SELECT * FROM read_parquet('{(fraction_dir / f'{table}.parquet').as_posix()}')"
            )
        n = oc.execute(
            "SELECT COUNT(*) FROM lineitem l "
            "JOIN orders o ON l.l_orderkey = o.o_orderkey "
            "JOIN customer c ON o.o_custkey = c.c_custkey"
        ).fetchone()[0]
        assert n > 0
    finally:
        oc.close()


def test_register_workload_from_duckdb_is_idempotent(tmp_path):
    from synnodb.workloads.byo_workload import register_workload_from_duckdb

    con = duckdb.connect()
    _make_synthetic(con)
    managed = tmp_path / "subsets"
    kwargs = dict(
        name="synthetic_byo_idem",
        con=con,
        queries_json=_SYNTHETIC_QUERIES,
        managed_root=managed,
        downscale_fractions=(0.2,),
        whole_table_threshold=10,
    )
    v1 = register_workload_from_duckdb(**kwargs).dataset_version
    # Identical data yields the same deterministic fingerprint, so the second call reuses the
    # existing snapshot/subsets (the default) and reports the same version.
    v2 = register_workload_from_duckdb(**kwargs).dataset_version
    con.close()
    assert v1 == v2


def test_factory_provider_resolves_fraction_subsets(tmp_path):
    """The factory's OLAPWorkloadProvider must resolve the downscaler's ``fraction<f>`` subsets
    the same way it resolves legacy ``sf<N>`` ones - the integration point that lets the whole
    factory run unchanged against DuckDB-derived subsets."""
    from synnodb.tools.run_tool_mode import RunToolMode
    from synnodb.utils.utils import DBStorage
    from synnodb.workloads.byo_workload import register_workload_from_duckdb
    from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider

    con = duckdb.connect()
    _make_synthetic(con)
    managed = tmp_path / "subsets"
    register_workload_from_duckdb(
        name="synthetic_factory",
        con=con,
        queries_json=_SYNTHETIC_QUERIES,
        managed_root=managed,
        downscale_fractions=(0.2,),
        whole_table_threshold=10,
        serve_from="parquet",  # this test exercises the parquet-subset factory path specifically
    )
    con.close()

    prov = OLAPWorkloadProvider(
        benchmark="synthetic_factory",
        base_parquet_dir=managed,
        db_storage=DBStorage.IN_MEMORY,
        query_ids=["1"],
    )
    # Only the benchmark subset exists after sync; the fractional rung is built lazily by prepare().
    assert "fraction0.2" not in dict(prov._datasets_on_disk())
    prov.prepare()
    # every candidate subset present on disk is discovered under its fraction directory
    on_disk = dict(prov._datasets_on_disk())
    assert "fraction0.2" in on_disk and "fraction1" in on_disk

    # the fast-check sweep mints a parquet dir that actually exists (the path built at
    # provider load time, previously hardcoded to ``sf<N>``)
    batches = prov.produce_workload(
        RunToolMode.FAST_CHECK, query_ids=["1"], num_threads=1, core_ids=None
    )
    assert batches
    for batch in batches:
        subset_dir = Path(batch.exec_settings.parquet_dir)
        assert subset_dir.name.startswith("fraction")
        assert (subset_dir / "lineitem.parquet").exists()
