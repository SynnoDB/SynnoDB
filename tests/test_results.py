"""The stage result objects carry the real artifact, not just a run id."""

from pathlib import Path

import pytest

from synnodb import BaseImplementation, StoragePlan
from synnodb.api import _as_arg, _build_base_impl, _build_storage_plan


def test_storage_plan_carries_the_document(tmp_path):
    (tmp_path / "storage_plan.txt").write_text("PLAN BODY", encoding="utf-8")
    plan = _build_storage_plan("rid", "snaphash", tmp_path, None, {})
    assert isinstance(plan, StoragePlan)
    assert plan.text == "PLAN BODY"
    assert str(plan) == "PLAN BODY"          # the object IS the document
    assert plan.path == tmp_path / "storage_plan.txt"
    assert plan.run_id == "rid"
    assert plan.snapshot_hash == "snaphash"  # W&B-free chaining token
    assert _as_arg(plan) == "rid"            # chains on its run id


def test_storage_plan_missing_file_is_empty_not_an_error(tmp_path):
    plan = _build_storage_plan(None, None, tmp_path, None, {})
    assert plan.text == ""


def test_base_impl_collects_generated_engine_files(tmp_path):
    (tmp_path / "db_loader.cpp").write_text("loader", encoding="utf-8")
    (tmp_path / "query_impl.hpp").write_text("hdr", encoding="utf-8")
    (tmp_path / "notes.md").write_text("ignored", encoding="utf-8")  # not .cpp/.hpp
    impl = _build_base_impl("rid2", "deadbeef", tmp_path, None, {})
    assert isinstance(impl, BaseImplementation)
    assert set(impl.files) == {"db_loader.cpp", "query_impl.hpp"}
    assert impl.file("db_loader.cpp") == "loader"
    assert impl.loader == "loader"
    assert impl.snapshot_hash == "deadbeef"


def test_artifact_without_run_id_cannot_chain():
    plan = StoragePlan(None, Path("x"), None, Path("x/storage_plan.txt"), "")
    assert not plan                          # falsy: nothing to chain
    with pytest.raises(ValueError, match="cannot chain"):
        _as_arg(plan)
