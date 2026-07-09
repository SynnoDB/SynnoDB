"""SynnoDB.check_ram_for_sf: an in-memory build needs IN_MEMORY_RAM_FACTOR (3x) the
dataset's on-disk parquet size in available RAM. Uses a bring-your-own workload so the
dataset lives under tmp_path, and a stubbed psutil so "available RAM" is deterministic."""

from __future__ import annotations

from types import SimpleNamespace

import psutil
import pytest

from synnodb.api import SynnoDB
from synnodb.ram_check import IN_MEMORY_RAM_FACTOR
from synnodb.workloads.byo_workload import register_workload_from_dir
from synnodb.workloads.workload_spec import find_sf_dir


@pytest.fixture
def ramshop(tmp_path):
    """A registered BYO workload with an sf1 dataset of known on-disk size."""
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    sf1 = tmp_path / "data" / "sf1"
    sf1.mkdir(parents=True)
    pq.write_table(pa.table({"u_id": [1, 2, 3]}), sf1 / "users.parquet")
    pq.write_table(pa.table({"e_id": [1, 2, 3, 4]}), sf1 / "events.parquet")

    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "1.sql").write_text("SELECT count(*) AS n FROM events\n")

    spec = register_workload_from_dir("ramshop", sql_dir, tmp_path / "data")
    dataset_bytes = sum(
        (sf1 / f"{t}.parquet").stat().st_size for t in ("users", "events")
    )
    return SynnoDB(workload="ramshop", workspace="out"), spec, dataset_bytes


def _set_available_ram(monkeypatch, available: int) -> None:
    monkeypatch.setattr(
        psutil, "virtual_memory", lambda: SimpleNamespace(available=available)
    )


def test_sufficient_ram(ramshop, monkeypatch):
    db, _, dataset_bytes = ramshop
    _set_available_ram(monkeypatch, int(dataset_bytes * IN_MEMORY_RAM_FACTOR) + 1)
    check = db.check_ram_for_sf(1)
    assert check.sufficient and bool(check)
    assert check.dataset_bytes == dataset_bytes
    assert check.required_bytes == int(dataset_bytes * IN_MEMORY_RAM_FACTOR)


def test_insufficient_ram(ramshop, monkeypatch):
    db, _, dataset_bytes = ramshop
    _set_available_ram(monkeypatch, int(dataset_bytes * IN_MEMORY_RAM_FACTOR) - 1)
    check = db.check_ram_for_sf(1)
    assert not check.sufficient and not bool(check)


def test_boundary_exactly_3x_is_sufficient(ramshop, monkeypatch):
    db, _, dataset_bytes = ramshop
    _set_available_ram(monkeypatch, int(dataset_bytes * IN_MEMORY_RAM_FACTOR))
    assert db.check_ram_for_sf(1).sufficient


def test_float_sf_resolves_int_dir(ramshop, monkeypatch):
    """sf=1.0 must find the sf1 directory (int/float name tolerance)."""
    db, _, dataset_bytes = ramshop
    _set_available_ram(monkeypatch, 2**62)
    assert db.check_ram_for_sf(1.0).dataset_bytes == dataset_bytes


def test_missing_sf_raises(ramshop, monkeypatch):
    db, _, _ = ramshop
    _set_available_ram(monkeypatch, 2**62)
    with pytest.raises(FileNotFoundError, match="No subset for 99"):
        db.check_ram_for_sf(99)


def test_missing_table_raises(ramshop, monkeypatch, tmp_path):
    db, _, _ = ramshop
    _set_available_ram(monkeypatch, 2**62)
    (tmp_path / "data" / "sf1" / "events.parquet").unlink()
    with pytest.raises(FileNotFoundError, match="events"):
        db.check_ram_for_sf(1)


def test_report_string(ramshop, monkeypatch):
    db, _, _ = ramshop
    _set_available_ram(monkeypatch, 0)
    assert "insufficient" in str(db.check_ram_for_sf(1))


def test_find_sf_dir_none_without_fallback(tmp_path):
    """find_sf_dir must not silently fall back to some other sf* directory."""
    (tmp_path / "sf1").mkdir()
    assert find_sf_dir(tmp_path, 1) == tmp_path / "sf1"
    assert find_sf_dir(tmp_path, 2) is None


# ---- the pipeline preflight gate (WorkloadProvider.preflight_ram_check) -----


def _provider(tmp_path, db_storage=None):
    from synnodb.utils.utils import DBStorage
    from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider

    return OLAPWorkloadProvider(
        benchmark="ramshop",
        base_parquet_dir=tmp_path / "data",
        db_storage=db_storage or DBStorage.IN_MEMORY,
    )


def test_preflight_measures_the_dataset(ramshop, monkeypatch, tmp_path):
    _, _, dataset_bytes = ramshop
    _set_available_ram(monkeypatch, int(dataset_bytes * IN_MEMORY_RAM_FACTOR))
    check = _provider(tmp_path).preflight_ram_check()
    assert check is not None and check.sufficient
    assert check.dataset_bytes == dataset_bytes


def test_preflight_reports_insufficient_ram(ramshop, monkeypatch, tmp_path):
    _, _, dataset_bytes = ramshop
    _set_available_ram(monkeypatch, int(dataset_bytes * IN_MEMORY_RAM_FACTOR) - 1)
    check = _provider(tmp_path).preflight_ram_check()
    assert check is not None and not check.sufficient


def test_preflight_none_for_disk_backed_storage(ramshop, monkeypatch, tmp_path):
    """Disk-backed runs do not ingest the dataset into RAM: nothing to gate."""
    from synnodb.utils.utils import DBStorage

    _set_available_ram(monkeypatch, 0)
    assert _provider(tmp_path, DBStorage.SSD).preflight_ram_check() is None


def test_preflight_none_when_no_parquet_on_disk(ramshop, monkeypatch, tmp_path):
    """SFs without data cannot be measured; the gate must not invent a failure."""
    import shutil

    provider = _provider(tmp_path)  # construct before the data disappears
    _set_available_ram(monkeypatch, 0)
    shutil.rmtree(tmp_path / "data" / "sf1")
    assert provider.preflight_ram_check() is None


def test_preflight_ignores_datasets_not_in_the_workload_spec(
    ramshop, monkeypatch, tmp_path
):
    """A stray sf* dataset the workload spec does not reference is not gated on:
    the check measures the workload's own scale factor, not whatever else happens
    to sit in the parquet root. (ramshop's spec references only sf1.)"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    _, _, sf1_bytes = ramshop
    sf2 = tmp_path / "data" / "sf2"
    sf2.mkdir()
    pq.write_table(pa.table({"u_id": list(range(1000))}), sf2 / "users.parquet")
    pq.write_table(pa.table({"e_id": list(range(1000))}), sf2 / "events.parquet")

    # Enough RAM for the workload's sf1 but not the larger, unrelated sf2.
    _set_available_ram(monkeypatch, int(sf1_bytes * IN_MEMORY_RAM_FACTOR))
    check = _provider(tmp_path).preflight_ram_check()
    assert check is not None and check.label == "sf1" and check.sufficient
    assert check.dataset_bytes == sf1_bytes


def test_preflight_gates_on_largest_spec_scale_factor_present(monkeypatch, tmp_path):
    """When the spec references several scale factors, the check measures the
    largest one whose dataset is present - an in-memory run holds one SF at a
    time, so that is the peak requirement."""
    from synnodb.utils.utils import DBStorage
    from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider

    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    data = tmp_path / "data"
    for sf, n in ((1, 3), (2, 1000)):
        sf_dir = data / f"sf{sf}"
        sf_dir.mkdir(parents=True)
        pq.write_table(pa.table({"u_id": list(range(n))}), sf_dir / "users.parquet")
        pq.write_table(pa.table({"e_id": list(range(n))}), sf_dir / "events.parquet")

    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "1.sql").write_text("SELECT count(*) AS n FROM events\n")
    register_workload_from_dir("ramshop_multi", sql_dir, data, scale_factors=(1, 2))

    sf2_bytes = sum(
        (data / "sf2" / f"{t}.parquet").stat().st_size for t in ("users", "events")
    )
    _set_available_ram(monkeypatch, int(sf2_bytes * IN_MEMORY_RAM_FACTOR) - 1)
    provider = OLAPWorkloadProvider(
        benchmark="ramshop_multi",
        base_parquet_dir=data,
        db_storage=DBStorage.IN_MEMORY,
    )
    check = provider.preflight_ram_check()
    assert check is not None and check.label == "sf2" and not check.sufficient
    assert check.dataset_bytes == sf2_bytes
