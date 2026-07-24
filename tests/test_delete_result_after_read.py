"""A result file must be deleted the instant its single consumer has read it - the engine
writes ``result_<req_id>.arrow``, the wrapper reads it into memory, and it is gone from disk
immediately. This holds on both consumption paths: verification (compare to DuckDB) and serving
(hand the table back to the caller). No result file is ever left behind for a later run to read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pyarrow as pa

from synnodb.router.process_engine import ProcessEngine
from synnodb.tools.validate.run_and_check_queries import (
    Measurement,
    check_output_correctness,
)
from synnodb.workloads.workload_provider import (
    ExecSettings,
    GeneralSystemConfig,
    QueryBatch,
    QueryEntry,
    format_args_element,
)


def _write_arrow(path: Path, table: pa.Table) -> None:
    with pa.OSFile(str(path), "wb") as sink:
        with pa.ipc.new_file(sink, table.schema) as writer:
            writer.write_table(table)


# --------------------------------------------------------------------------- serving path


class _FakeQueryResult:
    def __init__(self, req_id: str) -> None:
        self.req_id = req_id
        self.error = ""
        self.elapsed_ms = 1.0


class _FakeRunResult:
    query_results = None
    response = ""
    stderr = ""


class _FakeRunner:
    """Stands in for HotpatchProc: writes the engine's Arrow result where the real engine would,
    then returns a per-query result vector - so ProcessEngine.run exercises the real read+delete."""

    def __init__(self, table: pa.Table) -> None:
        self._table = table

    def run(self, *, timeout: int, query_lines, run_env: Mapping[str, str]) -> Any:
        req_id = query_lines[0].split()[1]
        result_dir = Path(run_env["SYNNODB_RESULT_DIR"])
        _write_arrow(result_dir / f"result_{req_id}.arrow", self._table)
        out = _FakeRunResult()
        out.query_results = [_FakeQueryResult(req_id)]
        return out

    def is_running(self) -> bool:
        return True


def test_serving_path_deletes_result_after_read(tmp_path, monkeypatch):
    table = pa.table({"n": pa.array([42], pa.int64())})
    eng = ProcessEngine("e", tmp_path, "/data/sf1")
    monkeypatch.setattr(eng, "_runner", lambda: _FakeRunner(table))

    got, _server_ms = eng.run("1", {})

    assert got.column("n").to_pylist() == [42]
    req_id = format_args_element("1", {}).split()[1]
    assert not (tmp_path / "results" / f"result_{req_id}.arrow").exists()
    # Nothing at all is left in the results directory.
    assert list((tmp_path / "results").glob("result*")) == []


# ----------------------------------------------------------------------- verification path


class _RefResult:
    def __init__(self, table: pa.Table) -> None:
        self.result = table
        self.exec_time_ms = 1.0


class _FakeQueryExecutionCache:
    """Returns a fixed DuckDB reference table for every query in the batch."""

    def __init__(self, reference: pa.Table) -> None:
        self._reference = reference

    def lookup_or_execute_query_batch(self, *, system, batch) -> list:
        return [_RefResult(self._reference) for _ in batch.query_list]


def _query_batch(req_id: str) -> QueryBatch:
    entry = QueryEntry(
        benchmark=None,
        query_id="1",
        sql="SELECT n FROM t",
        query_args=f"1 {req_id}",
        placeholders={},
        order_by_info=[],
    )
    return QueryBatch(
        query_list=[entry],
        benchmark=None,
        exec_settings=ExecSettings(),
        cli_call_args="",
        general_system_config=GeneralSystemConfig(
            memory_limit_mb=None, num_threads=1, core_ids=None
        ),
        timeout_s=60,
        extra_env={},
    )


def _run_check(out_path: Path, req_id: str, reference: pa.Table):
    return check_output_correctness(
        exec_settings=ExecSettings(),
        query_batch=_query_batch(req_id),
        measurements=[Measurement(query_id="1", req_id=req_id, exec_time=1.0)],
        out_path=out_path,
        cmd=None,
        stop_on_first_error=True,
        all_query_ids=[
            "1",
            "2",
        ],  # > executed set: skip the all-queries wandb plot branch
        stdout=None,
        stderr=None,
        trace_mode=False,
        query_execution_cache=_FakeQueryExecutionCache(reference),
    )


def test_verification_path_deletes_result_after_read(tmp_path):
    out_path = tmp_path / "results"
    out_path.mkdir()
    req_id = "req_1_deadbeef0000"
    table = pa.table({"n": pa.array([42], pa.int64())})
    res_path = out_path / f"result_{req_id}.arrow"
    _write_arrow(res_path, table)

    output = _run_check(out_path, req_id, reference=table)

    assert output.correct is True
    # Read once, then gone - nothing stale is left for a later run to misvalidate.
    assert not res_path.exists()


def test_verification_path_deletes_result_even_on_read_failure(tmp_path):
    out_path = tmp_path / "results"
    out_path.mkdir()
    req_id = "req_1_deadbeef0000"
    res_path = out_path / f"result_{req_id}.arrow"
    res_path.write_bytes(b"not a valid arrow file")  # forces a read error

    output = _run_check(
        out_path, req_id, reference=pa.table({"n": pa.array([42], pa.int64())})
    )

    assert output.correct is False  # surfaced as a read error, not a crash
    assert not res_path.exists()  # a corrupt result is cleaned up too
