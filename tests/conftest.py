"""Shared pytest fixtures for the SynnoDB test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _register_builtin_workloads():
    """Register the demo workloads (TPC-H, CEB) that live outside the core package.

    The core ``synnodb`` package is workload-agnostic and ships no built-in workload; the
    concrete workloads live under ``tutorials/workloads/*`` and are registered from the
    outside. Much of the suite drives runs against ``"tpch"``/``"ceb"``, so register them
    once here - exactly the way an application or notebook would before using them.
    """
    from tutorials.workloads.ceb.synnodb_workload import register as register_ceb
    from tutorials.workloads.tpch.synnodb_workload import register as register_tpch

    register_tpch()
    register_ceb()


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
