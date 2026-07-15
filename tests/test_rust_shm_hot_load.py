"""End-to-end: a Rust engine served over the /dev/shm zero-copy hot-load plane.

The Rust analogue of ``test_shm_hot_load.py``. It scaffolds a Rust TPC-H engine,
fills it with the hand-written reference engine in ``tests/rust/_engine_fixture``,
builds it (host + Rust plugins), hands a live DuckDB connection's tables to it
zero-copy over ``/dev/shm`` via ``ShmHotLoadEngine``, and cross-checks Q1/Q6
exactly against DuckDB -- exercising the generated loader's shm branch and
``synno_rt::shm::read_table`` inside a real engine.

Opt-in: a full cargo build is minutes, so this runs only when
``SYNNO_RUN_RUST_E2E=1`` (and cargo + the TPC-H parquet are present). The fast,
always-on gate for the shm *read* logic is ``test_cross_language_conformance.py``
(the Rust loader reads a Python-written segment identically to the C++ loader).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "rust" / "_engine_fixture"
TABLES = [
    "customer", "lineitem", "nation", "orders",
    "part", "partsupp", "region", "supplier",
]

Q1 = """select l_returnflag, l_linestatus, sum(l_quantity) as sum_qty,
 sum(l_extendedprice) as sum_base_price,
 sum(l_extendedprice*(1-l_discount)) as sum_disc_price,
 sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge,
 avg(l_quantity) as avg_qty, avg(l_extendedprice) as avg_price,
 avg(l_discount) as avg_disc, count(*) as count_order
 from lineitem where l_shipdate <= date '1998-12-01' - interval '90' day
 group by 1, 2 order by 1, 2"""
Q6 = """select sum(l_extendedprice*l_discount) as revenue from lineitem
 where l_shipdate >= date '1994-01-01' and l_shipdate < date '1994-01-01' + interval '1' year
 and l_discount between 0.05 and 0.07 and l_quantity < 24"""


def _skip_reasons() -> str | None:
    if os.environ.get("SYNNO_RUN_RUST_E2E") != "1":
        return "opt-in only (set SYNNO_RUN_RUST_E2E=1); a full cargo build is slow"
    if shutil.which("cargo") is None:
        return "no cargo toolchain"
    return None


def _lineitem_dir() -> Path | None:
    from synnodb import settings

    try:
        root = settings.get_data_dir() / "workloads" / "tpch" / "tpch_parquet"
    except RuntimeError:
        return None
    for sf in ("sf1", "sf2", "sf5"):
        if (root / sf / "lineitem.parquet").exists():
            return root / sf
    return None


def test_rust_engine_served_over_shm_matches_duckdb(tmp_path):
    reason = _skip_reasons()
    if reason:
        pytest.skip(reason)
    pq = _lineitem_dir()
    if pq is None:
        pytest.skip("no TPC-H parquet on this machine")

    import duckdb

    from synnodb.cpp_runner.compiler.cargo_builder import CargoBuilder
    from synnodb.cpp_runner.prepare_repo.prepare_features import PrepareFeatures
    from synnodb.cpp_runner.prepare_repo.prepare_workspace_rust import (
        RustPrepareWorkspace,
    )
    from synnodb.router.adapt import results_equal
    from synnodb.router.process_engine import ShmHotLoadEngine
    from synnodb.utils.utils import DBStorage
    from synnodb.workloads.workload_provider_olap import (
        OLAPWorkload,
        OLAPWorkloadProvider,
    )

    ws = tmp_path / "engine"
    ws.mkdir()

    # Scaffold a Rust TPC-H engine, then fill it with the reference fixture.
    provider = OLAPWorkloadProvider(
        benchmark=OLAPWorkload.TPCH,
        base_parquet_dir=pq.parent,
        db_storage=DBStorage.IN_MEMORY,
        query_ids=["1", "6"],
    )
    prep = RustPrepareWorkspace(
        workload_provider=provider,
        workspace_dir=ws,
        git_snapshotter=None,
        db_storage=DBStorage.IN_MEMORY,
    )
    features = PrepareFeatures(language="rust").resolve(DBStorage.IN_MEMORY)
    for name, content in prep.build_scaffold_files(features).items():
        p = ws / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    shutil.copy(FIXTURE / "builder_lib.rs", ws / "builder" / "src" / "lib.rs")
    for q in ("q1", "q6", "dates"):
        shutil.copy(FIXTURE / f"{q}.rs", ws / "query" / "src" / f"{q}.rs")
    lib = ws / "query" / "src" / "lib.rs"
    text = lib.read_text()
    if "pub mod dates;" not in text:
        lib.write_text(text.replace("pub mod args;", "pub mod args;\npub mod dates;"))

    builder = CargoBuilder(ws)
    builder.set_compile_options(optimize=False)
    err = builder.build()
    assert err is None, f"engine build failed:\n{err}"

    # Hand the live connection's tables to the engine over /dev/shm and cross-check.
    con = duckdb.connect()
    staged = {
        t: con.execute(
            f"SELECT * FROM read_parquet('{pq}/{t}.parquet')"
        ).to_arrow_table()
        for t in TABLES
    }
    con.execute(f"CREATE VIEW lineitem AS SELECT * FROM read_parquet('{pq}/lineitem.parquet')")

    eng = ShmHotLoadEngine(engine_id="rust-shm-e2e", workspace=ws)
    try:
        eng.ingest(staged)
        for qid, ph, sql in [
            ("1", {"DELTA": 90}, Q1),
            ("6", {"DATE": "1994-01-01", "DISCOUNT": 0.06, "QUANTITY": 24}, Q6),
        ]:
            timed = eng.run(qid, ph)
            got = getattr(timed, "table", None)
            got = got if got is not None else timed[0]
            ref = con.execute(sql).to_arrow_table()
            assert results_equal(got, ref, ordered=True), f"Q{qid} diverges from DuckDB over shm"
    finally:
        eng.close()
