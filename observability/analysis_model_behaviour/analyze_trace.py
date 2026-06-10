from observability.plots.classify_bespoke_storage.llms import (
    cost,
    count_tokens,
    execute,
)
from observability.plots.utils.per_stage_data_prep import load_wandb_data


def get_run_info(
    wandb_run: str, start_turn: int = 0, end_turn: int = -1
) -> tuple[str, str]:
    # 1: get wandb data for the run
    history_dict, config_dict, summary_dict, target_sf = load_wandb_data(
        runs=[(wandb_run, "run")], skip_cache=False
    )
    hist_df = history_dict["run"]

    # forward fill `current_prompt`
    hist_df["current_prompt"] = hist_df["current_prompt"].ffill()

    # sort by turn
    hist_df = hist_df.sort_values("turn").reset_index(drop=True)

    if start_turn > 0:
        hist_df = hist_df[hist_df["turn"] >= start_turn].reset_index(drop=True)
    if end_turn is not None and end_turn > 0:
        hist_df = hist_df[hist_df["turn"] <= end_turn].reset_index(drop=True)

    # group by current_prompt. sort=False preserves temporal order of prompts
    grouped = hist_df.groupby("current_prompt", sort=False)

    activity_summary = []

    for group in grouped:
        summary = {
            "prompt": group[0],
            "num_turns": len(group[1]),
        }

        sum_list = []
        sum_list_long = []
        # go over row by row
        for idx, row in group[1].iterrows():
            type = row["type"]

            if type == "shell":
                entry = f"shell call: {row['shell/commands']}"
                sum_list.append(entry)
                sum_list_long.append(entry)

            elif type == "apply_patch":
                sum_list.append(f"apply patch: {row['apply_patch/string'][:100]}...")
                sum_list_long.append(f"apply patch: {row['apply_patch/string']}")
            elif type == "compile":
                entry = f"compile: {'error' if row['compile/error'] else 'success'}"
                sum_list.append(entry)
                sum_list_long.append(entry)
            elif type == "run":
                entry = f"run: {'error' if row['run/error'] else 'success'}"
                sum_list.append(entry)
                sum_list_long.append(entry)

            elif type == "llm":
                if row["supervisor"] == True:
                    # supervisor feedback is included via new prompt
                    continue
                    # sum_list.append(
                    #     f"supervisor feedback: {'approved' if row['supervisor/approved'] else 'rejected'}"
                    # )
                else:
                    sum_list.append(f"llm: Output={row['llm/output_text'][:100]}...")
                    sum_list_long.append(f"llm: Output={row['llm/output_text']}")
            elif type == "compaction":
                sum_list.append("compaction")
                sum_list_long.append("compaction")

        summary["activity_summary"] = sum_list
        summary["activity_summary_long"] = sum_list_long
        activity_summary.append(summary)

    # assemble this as string
    activity_str = ""
    for i, summary in enumerate(activity_summary):
        activity_str += f"# Prompt {i}:\n {summary['prompt']}\n"
        activity_str += f"## Number of turns:\n{summary['num_turns']}\n"
        activity_str += "##Activity summary:\n"
        for activity in summary["activity_summary"]:
            activity_str += f"  - {activity}\n"
        activity_str += "\n"

    # assemble this as string
    activity_str_long = ""
    for i, summary in enumerate(activity_summary):
        activity_str_long += f"# Prompt {i}:\n {summary['prompt']}\n"
        activity_str_long += f"## Number of turns:\n{summary['num_turns']}\n"
        activity_str_long += "## Activity summary:\n"
        for activity in summary["activity_summary_long"]:
            activity_str_long += f"  - {activity}\n"
        activity_str_long += "\n"

    # count number of chars
    num_chars = len(activity_str)
    print(f"Activity summary: {num_chars} chars")

    return activity_str, activity_str_long


def get_prompt(mode: str, activity_str: str) -> tuple[str, str]:

    if mode == "analyze_turns":
        system_prompt = """You are an expert agent-trajectory reviewer.
Your goal is to reduce total turns by identifying prompt weaknesses and avoidable actions.

Context:
- Agent task: implement a storage layout and queries.
- Available tools: shell, apply_patch, compile, validate.
- Compilation is handled externally, so repeated agent-side compile checks are usually wasteful.

What to detect:
1) Prompt quality issues
    - underspecified goals
    - missing constraints / acceptance criteria
    - ambiguous scope
    - missing file/module targets
    - poor sequencing (no plan-first instruction)
2) Turn inflation patterns
    - random filesystem exploration (broad ls/find/grep without narrowing)
    - repeated compile/run/check loops with little code change
    - speculative edits or thrashing across files
    - repeated retries of similar failed actions
    - unnecessary tool calls
3) Concrete fixes
    - exact prompt rewrites to prevent waste
    - stricter execution strategy (plan, target files, stop conditions, batching edits)

Output requirements:
- Give me a summary what most time is being spent on.
- Be concise and evidence-based, citing prompt index and observed actions.
- Focus on highest-impact changes for reducing turns.
"""

        prompt = (
            "Analyze the activity summary below and give me a summary what most effort is spend on. Align this with the goal/task described in the prompts. Especially explain why turn counts are high and how to tune prompts to reduce turns.\n"
            "Pay special attention to underspecified prompts, random filesystem searching, and redundant compile/check behavior.\n\n"
            "Activity summary:\n\n"
            f"{activity_str}"
        )
    elif mode == "analyze_incorrect":
        system_prompt = """You are an expert agent-trajectory reviewer.
Your goal is to find out why the agent's final solution is incorrect by identifying prompt weaknesses and critical failure points in the trajectory.

Context:
- Agent task: implement a storage layout and queries.
- Available tools: shell, apply_patch, compile, validate.
- Compilation is handled externally, so repeated agent-side compile checks are usually wasteful.

What to detect:
1) Prompt quality issues
    - underspecified goals
    - missing constraints / acceptance criteria
    - ambiguous scope
    - missing file/module targets
    - poor sequencing (no plan-first instruction)
2) Potential causes for incorrect final solution
    - critical failure points in trajectory (e.g. failed compile with no code change, rejected supervisor feedback, failed test with no code change)
    - missed edge cases or constraints in prompt that could have prevented critical failure points

"""

        prompt = (
            "Analyze the activity summary below and give me a summary what went wrong. Align this with the goal/task described in the prompts.\n"
            "Activity summary:\n\n"
            f"{activity_str}"
        )
    elif mode == "overview":
        system_prompt = """You are an expert agent-trajectory reviewer.
Your goal is to give an overview of the agent's behavior and performance across the trajectory.
"""
        prompt = (
            "Analyze the activity summary below and give me an overview of the agent's behavior and performance across the trajectory. What was it doing? Was it successfull? What performance metrics were achieved?\n\nActivity summary:\n\n"
            f"{activity_str}"
        )
    elif mode == "analyze_speedup":
        system_prompt = """You are an expert agent-trajectory reviewer. Your goal is to identify why speedups or slowdowns happened in the trajectory and how to improve the prompts to achieve more consistent speedups."""
        prompt = (
            "Analyze the activity summary below and give me an analysis of why speedups or slowdowns happened in the trajectory. Align this with the goal/task described in the prompts. Pay special attention to the sequencing of actions, prompt quality, and potential wasted effort. How could the prompts be improved to achieve more consistent speedups?\n\nActivity summary:\n\n"
            f"{activity_str}"
        )
    elif mode == "analyze_ssd_issues":
        system_prompt = """You are an expert systems-performance reviewer studying why an LLM-driven code-generation agent struggles to produce fast multi-threaded SSD-backed OLAP code.

Context — what makes MT + SSD hard in this codebase:
- The dataset is larger than the buffer pool, so query runtime is dominated by bytes read from disk, not CPU cycles. Naive "parallelize the inner loop" wins are mostly irrelevant.
- Columns are read through a buffer pool via `pin_page(pg) -> ptr` / `unpin_page(pg)`. The buffer pool has a shared mutex around metadata; tiny chunks or contended pinning serialize threads.
- Two distinct parallelization patterns are needed depending on the bottleneck:
    * Pattern A — inner-loop row split inside one chunk. Correct only when CPU-bound. The main thread pins pages serially → workers idle while it blocks on `pread`.
    * Pattern B — outer-loop page-range partition. Each thread owns its pins, NVMe sees N concurrent read streams, queue depth grows. Required when I/O-bound.
- The agent must first DIAGNOSE the bottleneck from tracing data (buffer-pool read/pin timers, miss/bytes counters) and then choose the right pattern. Skipping this step is the most common failure.
- Common pitfalls the agent tends to hit:
    * Applying Pattern A in an I/O-bound regime → speedup << #cores, often near 1x.
    * False sharing on per-thread accumulators (not cache-line padded).
    * Pinning a page on one thread and unpinning on another (undefined under the framework).
    * Too-fine chunking → buffer-pool metadata mutex contention.
    * Scanning column A end-to-end then column B → K full passes through the buffer pool, eviction thrashing. Must co-process K columns over the same row range.
    * Spawning new std::threads per query call instead of dispatching to the existing thread pool.
    * Confusing per-thread tracing with aggregate wall time when reading the profile.
    * Optimizing CPU kernel (SIMD, branchless) while bandwidth-bound — wasted effort.
    * Regressing other queries when changing shared storage or shared helpers.
- The agent has tools: shell, apply_patch, compile, run (run also collects tracing data when called in trace mode). Multiple round trips are expensive; reading a file repeatedly or re-running with no edit between is pure waste.

What to detect in the trajectory:
1) Diagnosis behavior — did the agent inspect the single-threaded trace and explicitly classify I/O- vs CPU-bound BEFORE choosing a pattern? Or did it jump to a default pattern?
2) Pattern selection — which pattern (A or B, or something else) did it implement, and was that consistent with the actual bottleneck?
3) Mechanical correctness in the MT code — accumulator padding, pin/unpin ownership, chunk granularity, co-processing of multi-column scans, dispatch via the existing thread pool.
4) Iteration loop — when speedups did not materialize, did the agent re-classify the bottleneck, or did it keep tweaking the same pattern?
5) Regressions — did MT changes to shared structures regress other queries?
6) Prompt-induced confusion — places where the prompt was ambiguous, missing constraints, or led the agent down a dead end (e.g. unclear which tracing fields signal I/O-bound, no explicit "stop and re-diagnose" rule, etc.).

Output requirements:
- Start with a 3–5 sentence argumentation answering: WHY is fast MT-SSD code hard for this agent in general? Make the structural reasons explicit (I/O budget, buffer-pool mutex, diagnose-then-act dependency, multi-column page coupling, false sharing).
- Then walk through what the agent was actually trying to do, in order: diagnosis → pattern choice → implementation → measurement → iteration. Cite prompt index and concrete observed actions (commands, patches, run results).
- Explicitly state for each parallelization attempt: did it work? If not, what was the root cause (wrong pattern, mechanical bug, untouched bottleneck)?
- End with concrete, high-impact fixes — either to the prompt (add constraints / re-diagnosis rule / pattern-selection checklist) or to the framework (better tracing surfacing, clearer pin/unpin contract, safer thread-pool API). Prioritize by expected impact on MT speedup.
- Be evidence-based; quote shell commands and patch fragments where they illustrate a failure mode. Do not invent behavior not present in the trace.
"""

        prompt = (
            "Analyze the activity summary below from a multi-threaded SSD-backed optimization run.\n"
            "I want to understand why the agent has a hard time producing fast multi-threaded SSD code.\n\n"
            "Specifically answer, in this order:\n"
            "  1. What structurally makes fast MT-SSD code hard (independent of this trace) — the argumentation.\n"
            "  2. What the agent was actually trying to do across the trajectory: did it diagnose the bottleneck "
            "(I/O-bound vs CPU-bound) from tracing data before parallelizing? Which parallelization pattern did it pick "
            "(inner-loop row split vs outer-loop page-range partition vs something else)? Was the mechanical implementation correct "
            "(per-thread cache-line-padded accumulators, pin/unpin ownership on the same thread, chunk size, multi-column co-processing, "
            "thread-pool dispatch)?\n"
            "  3. Did it work? For each attempt, state the observed speedup direction and the most likely root cause if it failed.\n"
            "  4. Where do the prompts or the framework feedback fail the agent? What concrete prompt edits or framework changes would have "
            "the highest impact on MT-SSD speedup?\n\n"
            "Cite prompt indices and observed actions. Do not speculate beyond the trace.\n\n"
            "Activity summary:\n\n"
            f"{activity_str}"
        )
    else:
        raise ValueError(f"Unknown mode: {mode}")

    return system_prompt, prompt


# ── request helpers ───────────────────────────────────────────────────────────
def make_request(system: str, user: str, model: str, max_tokens: int = 100000) -> dict:
    # gpt-5 family: use "developer" role and max_completion_tokens (matches sample_request.py)
    return {
        "model": model,
        "messages": [
            {"role": "developer", "content": system},
            {"role": "user", "content": user},
        ],
        "max_completion_tokens": max_tokens,
    }


def extract_text(response: dict) -> str:
    return response["choices"][0]["message"]["content"]


def exec_llm(system_prompt: str, prompt: str, model: str) -> str:
    req = make_request(system_prompt, prompt, model, max_tokens=32000)

    tokens = count_tokens(req, silent=True)
    print(f"Request: ~{tokens:,} input tokens")

    resp = execute(req, budget=2.0, silent=False)
    c = cost(resp, silent=True)
    print(f"  → cost ${c:.4f}")  # cost() returns float for single dict response

    assert isinstance(resp, dict), "Expected single response dict"
    text = extract_text(resp)
    return text
