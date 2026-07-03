"""Shared pytest fixtures for the SynnoDB test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_snapshotter_repo(tmp_path_factory, monkeypatch):
    """Give every test its own git-snapshotter repository.

    The snapshot repo is a single shared cache at
    ``<SYNNO_DATA_DIR>/git_snapshotter`` (or ``SYNNO_SNAPSHOTTER_DIR``): all
    workspaces share one object store and ``refs/snapshots/*`` namespace there.
    Because ``SYNNO_DATA_DIR`` can be set process-wide by other tests,
    snapshotters built in different tests would otherwise share one repo and
    observe each other's refs. Pinning ``SYNNO_SNAPSHOTTER_DIR`` to a unique
    per-test directory keeps each test's snapshots isolated.
    """
    snapshotter_dir = tmp_path_factory.mktemp("snapshotter")
    monkeypatch.setenv("SYNNO_SNAPSHOTTER_DIR", str(snapshotter_dir))
