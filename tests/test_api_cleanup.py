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


def test_context_manager_deletes_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ws2").mkdir()
    with SynnoDB(workspace="ws2") as db:  # noqa: F841
        assert (tmp_path / "ws2").exists()
    assert not (tmp_path / "ws2").exists()


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
