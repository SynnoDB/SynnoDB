import os
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("SYNNO_DATA_DIR", "/tmp")

from tools.validate.query_validator_class import QueryValidator
from workloads.workload_provider import (
    ExecSettings,
    GeneralSystemConfig,
    QueryBatch,
    QueryEntry,
    Workload,
)


class DummyWorkload(Workload):
    TEST = "test"


class FakeSnapshotter:
    def __init__(self, current_hash: str = "start") -> None:
        self.current_hash = current_hash
        self.restored: list[str] = []

    def restore(self, snapshot_hash: str) -> None:
        self.current_hash = snapshot_hash
        self.restored.append(snapshot_hash)


def _batch(query_args: str, placeholders: dict[str, str] | None = None) -> QueryBatch:
    entry = QueryEntry(
        benchmark=DummyWorkload.TEST,
        query_id="1",
        sql="select * from lineitem where l_orderkey = 1",
        query_args=query_args,
        placeholders=placeholders or {"orderkey": "1"},
        order_by_info=[],
    )
    return QueryBatch(
        query_list=[entry],
        benchmark=DummyWorkload.TEST,
        exec_settings=ExecSettings(),
        cli_call_args="/data/sf1/",
        general_system_config=GeneralSystemConfig(
            memory_limit_mb=None,
            num_threads=1,
            core_ids=None,
        ),
        timeout_s=120,
        extra_env={},
    )


def _cache_hash_for(batch: QueryBatch, tmp_path: Path) -> str:
    validator = QueryValidator(
        validate_cache_dir=None,
        workspace_path=tmp_path,
        query_execution_cache=object(),  # type: ignore[arg-type]
        all_query_ids=["1"],
        git_snapshotter=FakeSnapshotter(),
    )
    _, _, cache_hash, _ = validator._check_answer_from_cache(
        skip_validate=False,
        other_config={"optimize": True, "memory_budget_mb": None},
        stop_on_first_error=True,
        compile_key_hash="compile-hash",
        query_batch=batch,
    )
    assert cache_hash is not None
    return cache_hash


def test_validation_cache_key_ignores_generated_request_ids(tmp_path: Path) -> None:
    first = _batch('1 req-20260615-1 "1"')
    second = _batch('1 req-20260615-2 "1"')

    assert _cache_hash_for(first, tmp_path) == _cache_hash_for(second, tmp_path)


def test_validation_cache_key_still_includes_query_semantics(tmp_path: Path) -> None:
    first = _batch('1 req-20260615-1 "1"')
    changed_entry = replace(first.query_list[0], placeholders={"orderkey": "2"})
    second = replace(first, query_list=[changed_entry])

    assert _cache_hash_for(first, tmp_path) != _cache_hash_for(second, tmp_path)
