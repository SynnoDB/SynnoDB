"""The generation prompts steer the engine toward a small in-memory footprint: at most
~10-15% over the DECOMPRESSED size of the loaded columns (not the compressed Parquet).
This is a prompt-only guardrail against ballooning builds (the naive 70 GB engine) - there is
no memory budget knob or enforcement, since the target scales itself to each engine's data.
"""

from __future__ import annotations

from synnodb.conversations.prompts_gen import (
    base_optimize_build,
    gen_storage_plan_prompt,
)
from synnodb.workloads.workload_provider import ExecSettings


def test_storage_plan_prompt_states_the_footprint_target():
    prompt = gen_storage_plan_prompt(
        "queries.json", "SCHEMA", "plan.txt", persistent_storage=False, num_threads=1
    )
    assert "10-15%" in prompt
    assert "in-memory footprint" in prompt
    # The baseline is the decompressed column size, not the compressed file.
    assert "compressed" in prompt


def test_optimize_build_prompt_states_the_footprint_tradeoff():
    prompt = base_optimize_build(
        builder_path_cpp="db_loader.cpp",
        builder_path_hpp="db_loader.hpp",
        current_ingest_time_ms=1234.0,
        current_exec_config=ExecSettings(),
        persistent_storage=False,
        allow_storage_restructuring=True,
        storage_plan_filename="storage_plan.txt",
        base_impl_todo_filename="base_impl_todo.txt",
    )
    assert "10-15%" in prompt
    # A memory-for-speed trade past the headroom must earn it with a large speedup.
    assert "large, measured speedup" in prompt
