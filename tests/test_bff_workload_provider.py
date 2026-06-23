import os
from pathlib import Path

os.environ.setdefault("SYNNO_DATA_DIR", "/tmp")

import pytest

from main import get_bff_bespoke_ssd_storage_dir, get_effective_db_storage
from tools.run_tool_mode import RunToolMode
from utils.cli_config import Usecase
from utils.utils import DBStorage
from run_ff_base import build_parser
from workloads.workload_provider_bff import (
    BFFExecSettings,
    BFFWorkload,
    BFFWorkloadProvider,
)


def test_run_ff_base_does_not_expose_db_storage() -> None:
    args = build_parser().parse_args(["--queries", "1"])

    assert not hasattr(args, "db_storage")


def test_bff_bespoke_ssd_storage_dir_is_workspace_local(tmp_path: Path) -> None:
    assert get_bff_bespoke_ssd_storage_dir(tmp_path) == tmp_path.absolute() / "tmp"


def test_bff_effective_db_storage_is_always_ssd() -> None:
    assert get_effective_db_storage(Usecase.BFF, DBStorage.IN_MEMORY) == DBStorage.SSD
    assert get_effective_db_storage(Usecase.BFF, DBStorage.SSD) == DBStorage.SSD


def test_olap_effective_db_storage_preserves_argument() -> None:
    assert (
        get_effective_db_storage(Usecase.OLAP, DBStorage.IN_MEMORY)
        == DBStorage.IN_MEMORY
    )


def test_provider_requires_bespoke_ssd_storage_dir() -> None:
    with pytest.raises(
        ValueError, match="BFFWorkloadProvider requires bespoke_ssd_storage_dir"
    ):
        BFFWorkloadProvider(
            benchmark=BFFWorkload.TPCH,
            base_parquet_dir=Path("/tmp/parquet"),
            bespoke_ssd_storage_dir=None,
        )


def test_fast_check_with_bespoke_storage_sets_storage_dir_per_batch(
    tmp_path: Path,
) -> None:
    provider = BFFWorkloadProvider(
        benchmark=BFFWorkload.TPCH,
        base_parquet_dir=Path("/tmp/parquet"),
        bespoke_ssd_storage_dir=tmp_path / "storage",
        memory_limit_mb=100,
    )

    batches = provider.produce_workload(
        run_mode=RunToolMode.FAST_CHECK,
        num_threads=1,
        core_ids=None,
        query_ids=["1"],
    )

    for batch, scale_factor in zip(batches, [1, 2], strict=True):
        storage_dir = tmp_path / "storage" / f"sf{scale_factor}"
        assert batch.extra_env == {
            "BUFFER_POOL_MB": "60",
            "STORAGE_DIR": str(storage_dir) + os.sep,
        }
        assert isinstance(batch.exec_settings, BFFExecSettings)
        assert batch.exec_settings.disk_db_dir == storage_dir
        assert (storage_dir / ".bespoke_storage_dir").is_file()
