"""Helpers for constructing publish-gate receipts in tests.

The publish API requires a :class:`ValidationReceipt`. Tests that exercise publish *plumbing*
(naming, atomic swap, discovery) rather than the gate itself build a passing receipt for the
workspace they publish via :func:`passing_receipt`. Tests of the gate's *refusal* behavior
construct receipts directly so they can set a single field wrong.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Sequence

from synnodb.cpp_runner.hotpatch.elf_build_id import read_build_id
from synnodb.workloads.validation_receipt import (
    PASS,
    PLANE_PARQUET,
    ValidatedQuery,
    ValidationReceipt,
    engine_build_ids,
)

# A real engine's `db` carries an NT_GNU_BUILD_ID (the compiler stamps it); the publish gate
# refuses an unidentifiable build. Fake test workspaces need a `db` with a real build-id, so we
# copy a system ELF that has one rather than write a stub byte string.
_BUILD_ID_DONORS = ("/bin/true", "/usr/bin/true", "/bin/ls", "/usr/bin/ls")


def _build_id_donor() -> "str | None":
    for donor in _BUILD_ID_DONORS:
        if Path(donor).exists() and read_build_id(donor):
            return donor
    return None


def write_fake_engine_db(db_path: "str | Path") -> None:
    """Create a fake engine `db` binary that carries a real build-id (by copying a system ELF), so
    the publish gate's build-id identity check is exercised, not bypassed."""
    donor = _build_id_donor()
    if donor is None:  # no Linux ELF with a build-id available (not expected on a real host)
        import pytest

        pytest.skip("no system binary with a build-id available for the fake engine fixture")
    shutil.copy2(donor, db_path)


def passing_receipt(
    workspace: "str | Path",
    query_ids: Sequence[str],
    *,
    planes: Sequence[str] = (PLANE_PARQUET,),
    scale_factors: Sequence[float] = (),
    dataset: str = "test",
) -> ValidationReceipt:
    """A pass receipt whose build-ids match *workspace*, covering *query_ids* on *planes*."""
    return ValidationReceipt(
        snapshot_id="test-snapshot",
        build_ids=engine_build_ids(workspace),
        validated_queries=tuple(ValidatedQuery(str(q), ()) for q in query_ids),
        coverage_policy="test",
        data_planes=tuple(planes),
        dataset=dataset,
        validated_scale_factors=tuple(float(s) for s in scale_factors),
        mode="test",
        live_run=True,
        verdict=PASS,
    )
