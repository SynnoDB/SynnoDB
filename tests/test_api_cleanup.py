"""Ephemeral-workspace cleanup on the SynnoDB facade (cleanup_workspace / context manager)."""
from __future__ import annotations

from pathlib import Path

from synnodb.api import SynnoDB


def test_cleanup_method_deletes_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / "eph_ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "db").write_text("binary")
    db = SynnoDB(workspace="eph_ws")
    assert ws.exists()
    db.cleanup()
    assert not ws.exists()
    db.cleanup()  # idempotent


def test_context_manager_keeps_workspace_by_default(tmp_path, monkeypatch):
    """Workspace deletion is opt-in: without cleanup_workspace the context manager must NOT
    delete the workspace, so `with SynnoDB(...) as db:` over the default ./output never erases
    generated artifacts."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ws2").mkdir()
    with SynnoDB(workspace="ws2") as db:  # noqa: F841
        assert (tmp_path / "ws2").exists()
    assert (tmp_path / "ws2").exists()


def test_context_manager_deletes_workspace_when_opted_in(tmp_path, monkeypatch):
    """With cleanup_workspace=True the context manager tears the workspace down on block exit."""
    monkeypatch.chdir(tmp_path)
    # Isolate __exit__ cleanup from the atexit/signal hooks (covered separately below).
    monkeypatch.setattr(SynnoDB, "_install_workspace_cleanup", lambda self: None)
    (tmp_path / "ws2b").mkdir()
    with SynnoDB(workspace="ws2b", cleanup_workspace=True) as db:  # noqa: F841
        assert (tmp_path / "ws2b").exists()
    assert not (tmp_path / "ws2b").exists()


def test_cleanup_workspace_flag_registers_atexit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registered = []
    import atexit

    monkeypatch.setattr(atexit, "register", lambda fn, *a, **k: registered.append(fn))
    db = SynnoDB(workspace="ws3", cleanup_workspace=True)
    assert db._cleanup_installed is True
    assert registered, "cleanup_workspace=True should register an atexit handler"
    # invoking the registered handler cleans the dir
    (tmp_path / "ws3").mkdir()
    registered[0]()
    assert not (tmp_path / "ws3").exists()
