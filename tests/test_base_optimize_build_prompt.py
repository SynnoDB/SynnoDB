"""
Regression tests for base_optimize_build() prompt generation.

Guards:
- All template variables are substituted (no leftover ${...}).
- storage_constraint and interface_compat_hint are empty for the early stage
  (allow_storage_restructuring=True) and populated for the late stage.
- interface_compat_hint references builder_path_hpp so the LLM knows which
  file's interface must be preserved.
- "Phase 8" is absent from the generated prompt (it was a tutorial-specific
  reference that was removed).
- _detect_hardware_context() returns a non-empty string.
"""
from __future__ import annotations

import re

from synnodb.conversations.prompts_gen import _detect_hardware_context, base_optimize_build
from synnodb.workloads.workload_provider import ExecSettings

_BUILDER_HPP = "db_loader.hpp"
_BUILDER_CPP = "db_loader.cpp"
_EXEC_SETTINGS = ExecSettings()
_STORAGE_PLAN = "storage_plan.txt"
_TODO = "base_impl_todo.txt"


def _build(persistent_storage: bool, allow_storage_restructuring: bool) -> str:
    return base_optimize_build(
        builder_path_cpp=_BUILDER_CPP,
        builder_path_hpp=_BUILDER_HPP,
        current_ingest_time_ms=12345.0,
        current_exec_config=_EXEC_SETTINGS,
        persistent_storage=persistent_storage,
        allow_storage_restructuring=allow_storage_restructuring,
        storage_plan_filename=_STORAGE_PLAN,
        base_impl_todo_filename=_TODO,
    )


def test_detect_hardware_context_non_empty():
    assert _detect_hardware_context().strip()


def test_no_leftover_template_variables():
    for persistent in (False, True):
        for restructure in (False, True):
            prompt = _build(persistent, restructure)
            leftover = re.findall(r"\$\{[^}]+\}", prompt)
            assert not leftover, f"Unsubstituted variables {leftover} in prompt (persistent={persistent}, restructure={restructure})"


def test_early_stage_has_no_storage_constraint(persistent_storage=False):
    for persistent in (False, True):
        prompt = _build(persistent, allow_storage_restructuring=True)
        assert "do not skip rows or columns" not in prompt
        assert "interface that existing query files" not in prompt


def test_late_stage_has_storage_constraint():
    for persistent in (False, True):
        prompt = _build(persistent, allow_storage_restructuring=False)
        assert "do not skip rows or columns" in prompt


def test_late_stage_interface_compat_hint_references_builder_hpp():
    for persistent in (False, True):
        prompt = _build(persistent, allow_storage_restructuring=False)
        assert _BUILDER_HPP in prompt
        assert "Interface compatibility" in prompt


def test_no_phase_8_reference():
    for persistent in (False, True):
        for restructure in (False, True):
            prompt = _build(persistent, restructure)
            assert "Phase 8" not in prompt, f"Tutorial-specific 'Phase 8' found (persistent={persistent}, restructure={restructure})"


def test_single_edit_instrumentation_removal_instruction():
    for persistent in (False, True):
        for restructure in (False, True):
            prompt = _build(persistent, restructure)
            assert "single edit" in prompt
