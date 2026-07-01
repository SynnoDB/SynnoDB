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

from synnodb.conversations.prompts_gen import (
    _detect_hardware_context,
    base_optimize_build,
)
from synnodb.workloads.workload_provider import ExecSettings

_BUILDER_HPP = "db_loader.hpp"
_BUILDER_CPP = "db_loader.cpp"
_EXEC_SETTINGS = ExecSettings()
_STORAGE_PLAN = "storage_plan.txt"
_TODO = "base_impl_todo.txt"


def _build(
    persistent_storage: bool,
    allow_storage_restructuring: bool,
    serving_threads: int | None = 8,
    memory_budget_mb: int | None = 16384,
) -> str:
    return base_optimize_build(
        builder_path_cpp=_BUILDER_CPP,
        builder_path_hpp=_BUILDER_HPP,
        current_ingest_time_ms=12345.0,
        current_exec_config=_EXEC_SETTINGS,
        persistent_storage=persistent_storage,
        allow_storage_restructuring=allow_storage_restructuring,
        storage_plan_filename=_STORAGE_PLAN,
        base_impl_todo_filename=_TODO,
        serving_threads=serving_threads,
        memory_budget_mb=memory_budget_mb,
    )


def test_detect_hardware_context_non_empty():
    assert _detect_hardware_context().strip()


def test_hardware_context_core_count_comes_from_affinity():
    from synnodb.utils.core_utils import get_cores_for_current_machine

    # Compute what the affinity-based count should be (same call as prompts_gen uses).
    usable_cores, _ = get_cores_for_current_machine(
        leave_core_0_out=False, allow_hyperthreading=True
    )
    ctx = _detect_hardware_context()
    assert f"{usable_cores} cores available for build parallelism" in ctx


def test_hardware_context_reports_configured_threads_and_budget_ceiling():
    # The optimizer must be anchored to the engine's operating envelope, not the raw host:
    # the configured serving parallelism and the memory budget framed as a hard ceiling.
    ctx = _detect_hardware_context(serving_threads=7, memory_budget_mb=4096)
    assert "7 query worker threads" in ctx
    assert "4 GB memory budget" in ctx
    assert "ceiling" in ctx


def test_hardware_context_omits_thread_claim_when_unset():
    # Without a configured degree we must not invent a serving-parallelism number.
    assert "worker threads" not in _detect_hardware_context()


def test_hardware_context_frames_total_ram_as_ceiling_without_budget():
    # With no explicit budget, total RAM is a shared ceiling, never free headroom.
    ctx = _detect_hardware_context(memory_budget_mb=None)
    # /proc/meminfo may be unreadable in some sandboxes; assert framing only when RAM shows.
    if "system RAM" in ctx:
        assert "shared ceiling" in ctx


def test_optimize_build_prompt_surfaces_threads_and_budget():
    for persistent in (False, True):
        prompt = _build(
            persistent,
            allow_storage_restructuring=True,
            serving_threads=9,
            memory_budget_mb=8192,
        )
        assert "9 query worker threads" in prompt
        assert "8 GB memory budget" in prompt


def test_no_leftover_template_variables():
    for persistent in (False, True):
        for restructure in (False, True):
            prompt = _build(persistent, restructure)
            leftover = re.findall(r"\$\{[^}]+\}", prompt)
            assert not leftover, (
                f"Unsubstituted variables {leftover} in prompt (persistent={persistent}, restructure={restructure})"
            )


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
            assert "Phase 8" not in prompt, (
                f"Tutorial-specific 'Phase 8' found (persistent={persistent}, restructure={restructure})"
            )


def test_single_edit_instrumentation_removal_instruction():
    for persistent in (False, True):
        for restructure in (False, True):
            prompt = _build(persistent, restructure)
            assert "single edit" in prompt


def test_plan_files_allowed_in_scope_constraint():
    for persistent in (False, True):
        for restructure in (False, True):
            prompt = _build(persistent, restructure)
            # The constraint block must list both plan files so the agent is not
            # forced to violate scope in order to follow the "read before profiling"
            # instruction.
            constraint_block = prompt[prompt.index("Constraints:") :]
            assert _STORAGE_PLAN in constraint_block
            assert _TODO in constraint_block
