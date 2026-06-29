"""Phase-1 conformance: `import synnodb as duckdb` must be a faithful, light drop-in.

The central invariant: with no engines registered (the default), SynnoDB behaves
byte-identically to DuckDB.
"""
from __future__ import annotations

import subprocess
import sys

import duckdb
import pytest

import synnodb
from synnodb.duckdb_compat.connection import SynnoError


# --------------------------------------------------------------------------- #
# Light runtime: importing the drop-in must not drag in the LLM factory stack.
# --------------------------------------------------------------------------- #
def test_import_is_light():
    script = (
        "import sys; import synnodb;"
        "heavy={'anthropic','openai','litellm','wandb','weave','agents',"
        "'openai_agents','psycopg2'};"
        "pulled=sorted(heavy & {m.split('.')[0] for m in sys.modules});"
        "factory=[m for m in sys.modules if m.startswith('synnodb.api')"
        " or m.startswith('synnodb.llm') or m.startswith('synnodb.conversations')];"
        "print(repr(pulled)); print(repr(factory))"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    pulled, factory = out.stdout.strip().splitlines()
    assert pulled == "[]", f"drop-in import pulled heavy deps: {pulled}"
    assert factory == "[]", f"drop-in import pulled factory modules: {factory}"


def test_lazy_factory_resolves_but_is_not_imported_eagerly():
    # The name resolves (so `from synnodb import SynnoDB` works) ...
    assert hasattr(synnodb, "SynnoDB")
    # ... but a fresh interpreter that only touches the drop-in never imports api.
    script = "import synnodb; import sys; print('synnodb.api' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


# --------------------------------------------------------------------------- #
# Namespace + exception + version parity.
# --------------------------------------------------------------------------- #
def test_namespace_parity():
    missing = {n for n in dir(duckdb) if not n.startswith("__")} - set(dir(synnodb))
    assert not missing, f"synnodb is missing DuckDB names: {sorted(missing)}"


def test_exceptions_are_duckdbs_own_classes():
    for name in ("CatalogException", "BinderException", "InvalidInputException", "Error"):
        assert getattr(synnodb, name) is getattr(duckdb, name)


def test_version_mirrors_duckdb():
    assert synnodb.__version__ == duckdb.__version__
    assert isinstance(synnodb.__synnodb_version__, str)


# --------------------------------------------------------------------------- #
# Zero-config equivalence with DuckDB.
# --------------------------------------------------------------------------- #
_QUERIES = [
    "SELECT 1 AS one, 'x' AS s",
    "SELECT a, b FROM t WHERE a >= 2 ORDER BY a",
    "SELECT count(*) AS c, sum(a) AS total FROM t",
    "SELECT b, count(*) FROM t GROUP BY b ORDER BY b",
]


def _seed(con):
    # Load through the underlying connection: writes are blocked on the routed surface,
    # and `con` may be a raw DuckDB connection or a SynnoConnection.
    raw = getattr(con, "duckdb", con)
    raw.execute("CREATE TABLE t(a INTEGER, b VARCHAR)")
    raw.execute("INSERT INTO t VALUES (1,'x'),(2,'y'),(3,'y'),(4,'z')")


@pytest.mark.parametrize("q", _QUERIES)
def test_zero_config_equivalence(q):
    d = duckdb.connect()
    s = synnodb.connect()
    _seed(d)
    _seed(s)
    assert s.execute(q).fetchall() == d.execute(q).fetchall()


def test_cursor_semantics_match_duckdb():
    s = synnodb.connect()
    _seed(s)
    s.execute("SELECT a FROM t ORDER BY a")
    assert s.fetchone() == (1,)
    assert s.fetchmany(2) == [(2,), (3,)]
    assert s.fetchall() == [(4,)]
    assert s.fetchone() is None  # consumed


def test_parameterized_passthrough():
    s = synnodb.connect()
    _seed(s)
    assert s.execute("SELECT b FROM t WHERE a = ?", [2]).fetchall() == [("y",)]


def test_df_and_arrow_egress():
    s = synnodb.connect()
    d = duckdb.connect()
    _seed(s)
    _seed(d)
    q = "SELECT a FROM t ORDER BY a"
    assert list(s.execute(q).df()["a"]) == [1, 2, 3, 4]
    # `arrow()` return-type parity with DuckDB (a RecordBatchReader in 1.5.x).
    assert type(s.execute(q).arrow()).__name__ == type(d.execute(q).arrow()).__name__
    # Materialized table via fetch_arrow_table/to_arrow_table.
    assert s.execute(q).fetch_arrow_table().column("a").to_pylist() == [1, 2, 3, 4]


# --------------------------------------------------------------------------- #
# Delegation: relational API / register / read are pure DuckDB (not routed).
# --------------------------------------------------------------------------- #
def test_relational_api_delegated():
    s = synnodb.connect()
    _seed(s)
    rel = s.sql("SELECT a FROM t WHERE a > 2")
    assert type(rel) is duckdb.DuckDBPyRelation
    assert rel.order("a").fetchall() == [(3,), (4,)]


def test_register_dataframe_delegated():
    pd = pytest.importorskip("pandas")
    s = synnodb.connect()
    frame = pd.DataFrame({"x": [10, 20, 30]})
    s.register("frame", frame)
    assert s.execute("SELECT sum(x) FROM frame").fetchall() == [(60,)]


# --------------------------------------------------------------------------- #
# Proxy honesty + escape hatch + lifecycle.
# --------------------------------------------------------------------------- #
def test_escape_hatch_and_isinstance_boundary():
    s = synnodb.connect()
    assert isinstance(s.duckdb, duckdb.DuckDBPyConnection)
    assert not isinstance(s, duckdb.DuckDBPyConnection)  # documented limitation


def test_context_manager_and_cursor():
    with synnodb.connect() as s:
        _seed(s)
        cur = s.cursor()
        assert cur.execute("SELECT count(*) FROM t").fetchall() == [(4,)]


def test_module_level_sql_and_execute():
    # Operates on the default in-memory connection, like duckdb.sql/execute.
    assert synnodb.execute("SELECT 40 + 2 AS v").fetchall() == [(42,)]
    assert synnodb.sql("SELECT 7 AS v").fetchall() == [(7,)]


def test_real_duckdb_error_propagates_unchanged():
    s = synnodb.connect()
    with pytest.raises(duckdb.CatalogException):
        s.execute("SELECT * FROM does_not_exist")


def test_write_parquet_and_csv_on_duckdb_fallback(tmp_path):
    """Writers serialise a DuckDB-fallback result (no engines registered), not just a routed
    one - the open result is pulled into typed Arrow via _materialize_current()."""
    import pyarrow.parquet as pq

    con = synnodb.connect()
    con.execute("SELECT 42 AS x, 'hi' AS y").write_parquet(str(tmp_path / "o.parquet"))
    con.execute("SELECT 7 AS x").write_csv(str(tmp_path / "o.csv"))
    assert pq.read_table(str(tmp_path / "o.parquet")).num_rows == 1
    assert "7" in (tmp_path / "o.csv").read_text()


def test_write_without_a_result_raises_clear_error(tmp_path):
    """Calling a writer before any result-producing query gives a clear SynnoError, not
    DuckDB's opaque 'No open result set'."""
    con = synnodb.connect()
    with pytest.raises(SynnoError, match="no result to write"):
        con.write_parquet(str(tmp_path / "none.parquet"))


def test_writer_does_not_mask_a_real_error(tmp_path, monkeypatch):
    """A genuine failure while materialising an existing result must propagate, not be hidden
    behind 'no result to write' (only DuckDB's no-open-result-set is rewritten)."""
    con = synnodb.connect()
    con.execute("SELECT 1 AS x")

    def boom(self):
        raise RuntimeError("arrow conversion blew up")

    monkeypatch.setattr(type(con), "_materialize_current", boom)
    with pytest.raises(RuntimeError, match="arrow conversion blew up"):
        con.write_parquet(str(tmp_path / "x.parquet"))
