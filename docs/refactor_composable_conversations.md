# Refactor: composable conversations on the SynnoDB class

## Goal

Users assemble and run new conversations directly from the `SynnoDB` class, with no
specialized conversation subclass, no fixed conversation name, and no named prepare
function:

```python
db = SynnoDB.in_memory(workload="tpch", queries="1,4,6")

def my_stages(ctx: ConvContext) -> list[StageItem]:
    return [
        AssertCorrect(),
        PromptStage(
            descriptor="inspect hot loops",
            get_prompt=lambda _exec_settings, _rt: (
                f"Profile {ctx.filenames.query_impl} and ..."),
            measure_performance_after_stage=False,
            auto_revert_on_regression=False,
        ),
        Compact(),
        PerQueryLoop(lambda qid, ctx: [
            PromptStage(
                descriptor=f"tune {qid}",
                # runtime and tracing data arrive exactly as they do today
                get_prompt_with_tracing=lambda _exec_settings, rt, trace: (
                    f"Query {qid} currently runs in {rt:.0f} ms.\n"
                    f"Trace:\n{trace}\nOptimize it ..."),
                max_turns=125,
                # defaults: measure after stage, auto-revert on regression
            ),
        ]),
        Benchmark(),
    ]

plan = ConversationPlan(
    name="myTuningPass",                    # run identity: naming, logging, caching
    prepare=PrepareFeatures(tracing=True),  # independent feature flags, see Phase 4
    stages=my_stages,
    parallelism=False,
)
result = db.run_synthesis(plan, start=base_impl)  # start: artifact | snapshot hash | None
```

`run_synthesis` is the **single entry point** for executing a conversation. The five
built-in stages (`createStoragePlan`, `createBaseImpl`, `runOptimLoop`,
`addMultiThreading`, `checkSfCorrectness`) become predefined `ConversationPlan`s built
from the same primitives, and the ergonomic `db.createBaseImpl(...)` methods keep
their signatures but become thin wrappers that resolve their chain inputs and call
`run_synthesis(plan, start=...)` internally. There is no separate dispatch path
for built-ins: one entry point, one execution pipeline, for predefined and
user-assembled conversations alike.

**Design principle: the existing stage design is the authoring surface.**
Today's `StaticStageConfig` / `DynamicStageConfig` already are good declarative
units - a descriptor, a `get_prompt(exec_settings, rt_before_ms)` /
`get_prompt_with_tracing(exec_settings, rt_before_ms, tracing_data)` callback that
receives the measured runtime for prompt assembly, measurement/revert flags,
`post_stage_validate`, and `max_turns`. They keep their fields, callback signatures,
and defaults; the only change is a rename of `StaticStageConfig` to **`PromptStage`**.
The refactor does not invent a parallel vocabulary; it
adds what is missing around them: typed markers, composite items (`PerQueryLoop` and
co.), a declarative prepare, and the `run_synthesis` entry point. This keeps churn
minimal and prompt bytes (= cache keys) trivially stable.

## Current state (summary)

- `SynnoDB.run()` (src/synnodb/api.py) resolves a `Stage` descriptor and hands a
  `RunConfig` to `run_conv_wrapper`/`main()` (src/synnodb/main.py), which assembles all
  collaborators, runs `stage.prepare` via `prepare_repo_and_load_snapshot`, then
  `stage.factory(ctx)` instantiates one of 7 specialized conversation classes.
- The generic engine already exists: `CheckpointedConversation._run_stages()` executes a
  declarative `list[StageConfig | str]`. The leaf classes mostly build stage lists and
  wrap them in imperative glue (per-query branch loop, SF override, baseline
  measurement, debug-logger lifecycle, stage-number bookkeeping).
- Prepare is four hardcoded functions (`prepare_storage_plan`, `prepare_base`,
  `prepare_optim`, `prepare_mt` in src/synnodb/cpp_runner/prepare_repo/prepare_olap.py)
  that compose three `PrepareWorkspace` primitives in fixed ways. `checkSfCorrectness`
  replays the source run's prepare by stage name.

## Guardrail before any phase: golden-prompt test

All LLM/tool caches key on prompt bytes; an accidental one-character prompt change
silently invalidates every cache. Before touching anything, add
`tests/test_golden_stage_lists.py`:

- For each of the 7 conversation classes, build the stage list with fixed synthetic
  inputs (mock `RunTool`/provider where needed), render every `get_prompt` /
  `get_prompt_with_tracing` with fixed arguments, and snapshot descriptors, ordering,
  markers, and prompt text into committed fixture files.
- Every later phase must keep this test green (byte-identical prompts, same order).
  Where a phase legitimately changes structure (e.g. stage numbering in Phase 3),
  update only the structural part of the fixture, never prompt text.

This test is also the equivalence proof when the leaf classes are deleted in Phase 6.

## Phase 1: typed stage items (kill the string mini-language)

New file `src/synnodb/conversations/stage_items.py`:

- `StageItem` base class.
- Marker items as frozen dataclasses: `Compact`, `Benchmark`, `ValidateOn`,
  `ValidateOff`, `ValidateStdoutOn`, `ValidateStdoutOff`, `SupervisionHorizon`. Each
  exposes its legacy string via a `.marker` property.
- `StaticStageConfig` moves here as **`PromptStage`** (rename only) and
  `DynamicStageConfig` moves unchanged; both gain `StageItem` as base. Fields,
  callback signatures (`get_prompt(exec_settings, rt_before_ms)`,
  `get_prompt_with_tracing(exec_settings, rt_before_ms, tracing_data)`), and defaults
  are kept as-is: runtime data already reaches every prompt through these signatures,
  and `get_prompt_with_tracing is not None` remains the signal that the engine must
  collect trace data before assembling the prompt. No alias is kept: all 7 in-tree
  builders switch to `PromptStage` in the same commit and `stage_config.py` is
  deleted (its content now lives in `stage_items.py`).

Changes:

- `CheckpointedConversation._run_stages` accepts `list[StageItem]` and lowers marker
  items to the existing strings when calling `_exec`. `handle_prompt`, the
  conversation-JSON persistence format, and the supervision skip set stay untouched:
  the strings remain the wire/persistence format, they just stop being the authoring
  format.
- All 7 stage-list builders emit typed items instead of raw strings.
- `SupervisionAgent.register_workload_info` is adapted to typed items (it only reads
  descriptors).

Risk: near zero; purely a typing seam. Golden test unchanged.

## Phase 2: one `run()` template, hooks for the rest

Move the shared `run()` skeleton into `CheckpointedConversation`:

```python
async def run(self):
    self.used = []
    with self._debug_logging(self.debug_category):        # DebugLogger lifecycle
        items = self.build_items()                        # abstract
        self._register_supervision(items)
        await self._pre_stages_hook()                     # temporary, removed in Phase 3
        await self._run_stages(items)
    if self.finish_interactive:                           # normalizes ask_to_finish_and_save
        return await self.ask_to_finish_and_save()
    return self.used
```

- Leaf classes keep only `build_items()` plus (temporarily) hook overrides for their
  imperative parts: InMem1's initial correctness assert, InMem2MT's baseline
  measurement and SF juggling, the optimization-loop invocation.
- `used` initialization moves into the base; delete the "must be initialized by child"
  assert in `conversation.py`.
- `finish_interactive` becomes an explicit constructor flag (True for optim/check-sf,
  False for base-impl/storage-plan, matching today's behavior exactly).
- The DebugLogger category comes from a `debug_category` attribute (today only
  base-impl and storage-plan set one; others pass None, preserving behavior).

## Phase 3: composite items + runner-owned stage counter

The heart of the refactor: make the remaining imperative control flow expressible as
data. New items and item attributes in `stage_items.py`, executed by the engine
(`PerQueryLoop` is the only composite; stage lists themselves stay flat):

1. `PerQueryLoop(build: Callable[[str, ConvContext], list[StageItem]],
   conversation_branching=True, end_of_ring_benchmark=True)` - absorbs
   `OptimizationConversation._run_optimization_loop`: branch creation per query
   (`create_conversation_branch_from_turn`), stage-major iteration with branch
   switching, pre-stage runtime measurement, tracing collection for stages that set
   `get_prompt_with_tracing`, `query_rt_log` maintenance, per-ring full benchmark,
   post-stage `Compact`. The loop feeds each stage's callback the measured
   `rt_before_ms` and fresh `tracing_data`, exactly as today. The stage authoring
   fields (`measure_performance_after_stage`, `auto_revert_on_regression`,
   `feedback_on_incorrect`, `measure_perf_qid`) stay untouched; only the engine's two
   overlapping measurement code paths (the loop's own vs the `measure_perf_qid` path
   in `_run_stage_with_revert_monitoring`) merge into one internal implementation.

   **Conversation branching is fully owned by the loop - including the branch
   anchor.** There is no user-facing `BranchAnchor` item: the anchor is an artifact
   of the SDK's branch semantics (`create_branch_from_turn(turn_nr)` copies turns
   *strictly before* `turn_nr`, so the turn at the branch point is sacrificed from
   every per-query branch), not a conversational step anyone should author. With
   `conversation_branching=True` the loop computes the branch point and decides
   itself whether it must first emit the no-op anchor turn (today's fixed anchor
   text from `in_mem_2_mt_conv.py`, kept byte-identical) so that only a disposable
   turn is excluded from the branches, never a meaningful one.

   This logic is **error-prone off-by-one territory; the implementation must stay as
   close to the current code as possible**, in this order: (a) first pin the SDK's
   turn-indexing semantics with a unit test against the session wrapper
   (`get_conversation_turns` vs `create_branch_from_turn` - which turn is excluded);
   (b) implement the loop's anchor decision so the built-in plans reproduce today's
   transcripts exactly (anchor emitted in the make-mt round, no anchor in optim
   round 1); (c) only if the decision cannot be derived robustly from SDK state,
   fall back to an explicit `branch_anchor: bool` parameter on `PerQueryLoop` set by
   the MT plans - still loop-internal, just not auto-detected.
2. `benchmark_sf: float | Literal["large_check"] | None = None` - **an attribute on
   the `StageItem` base, not a wrapper composite**, available to `PromptStage`,
   `DynamicStageConfig`, `PerQueryLoop`, and `Benchmark` alike. When set, the engine
   applies `set_benchmark_sf` before executing the item and restores the previous
   value in a `finally` (fixing the latent bug that today's restore in
   `in_mem_2_mt_conv.py` is skipped if the check raises). This replaces the
   `set_benchmark_sf`/restore pattern without introducing nested stage lists, so
   stage numbering, supervision registration, and the golden test all keep walking a
   flat list. Today's two use sites map directly: the MT large-SF check sets
   `benchmark_sf="large_check"` on its check stage and trailing `Benchmark()` item;
   the `checkSfCorrectness` plan sets `benchmark_sf=target_sf` on its single stage.
   Set/restore per item instead of per group is free - `set_benchmark_sf` is a plain
   field assignment on the provider.
3. `AssertCorrect(query_ids=None)` - the initial `_check_correctness` assert.

Engine changes:

- The runner owns a single monotonic `stage_nr` counter, incremented as items execute
  (composites increment through it). Delete every manual offset computation
  (`stage_nr_offset=2`, `branch_anchor_stage_nr`,
  `optim_stage_offset + len(query_ids) * per_query_stage_count`). Stage numbers feed
  only logging/dashboard, so renumbering is safe; verify the live-UI timeline still
  renders sensibly.
- `_run_optimization_loop` and the Phase 2 hooks are deleted; every leaf class is now
  purely `build_items()`.
- InMem2MT's single-threaded-baseline measurement becomes a declarative item
  (`MeasureBaselines(into="single_threaded_rt_ms")`) or a lazily-evaluated `ctx`
  value the stage prompts close over; decide during implementation, both stay
  declarative.

## Phase 4: prepare as independent feature flags

Prepare stops being a menu of four named functions and becomes a set of orthogonal
preparation features, each switchable on its own. A conversation author says *what the
workspace must have*, not *which pipeline stage they resemble*.

### 4.1 The feature set

Derived from what the three `PrepareWorkspace` primitives actually produce today:

| Feature | Backing primitive | What it provides |
|---|---|---|
| `scaffold` | `prepare(...)` | Framework files, templates, `queries.md`, build files. `scope="full"` or `scope="queries_md_only"` (storage-plan case). |
| `parallel_ready_impl` | `prepare(add_thread_pool_to_query_impl=...)` | Query impl scaffold generated in parallel-ready shape. `True` / `False` / `"auto"` (today's rule: in-memory storage => True). |
| `tracing` | `prepare_optim(...)` | `trace.hpp` plus tracing/flush instrumentation in the query impl. |
| `mt_helpers` | `prepare_mt(...)` | `thread_pool.hpp` and MT helper wiring. |
| `sample_trace` | `add_sample_trace` usecase arg | Sample trace file in the workspace. |
| `storage_plan_text` | `usecase_args["storage_plan"]` | Injects the plan text as `storage_plan.txt` into the clean workspace. Replaces the special case currently in `main()` (storage-plan text extraction, lines ~240-262). |

```python
@dataclass(frozen=True)
class PrepareFeatures:
    scaffold: Literal["full", "queries_md_only"] = "full"
    parallel_ready_impl: bool | Literal["auto"] = "auto"
    tracing: bool = False
    mt_helpers: bool = False
    sample_trace: bool = False
    storage_plan_text: str | None = None

    def to_json(self) -> str: ...
    @classmethod
    def from_json(cls, s: str) -> "PrepareFeatures": ...
```

New file: `src/synnodb/cpp_runner/prepare_repo/prepare_features.py`.

Built-in stages map onto features as:

| Stage | scaffold | parallel_ready_impl | tracing | mt_helpers |
|---|---|---|---|---|
| createStoragePlan | queries_md_only | False | False | False |
| createBaseImpl | full | auto | False | False |
| runOptimLoop | full | auto | True | False |
| addMultiThreading | full | True | True | True |
| checkSfCorrectness | (replays the source workspace's recorded features, see 4.4/4.5) |

Convenience constructors mirror these rows (`PrepareFeatures.storage_plan()`,
`.base()`, `.optim()`, `.mt()`) so callers rarely spell out flags.

### 4.2 The interpreter

`prepare_repo_and_load_snapshot` takes a `PrepareFeatures` and applies enabled features
in one canonical order - scaffold, tracing, mt_helpers - matching the order the legacy
functions used, so the concatenated artifacts string (a cache-key input via
`framework_code_content`) is assembled identically. The four functions in
`prepare_olap.py` are deleted once the interpreter is proven byte-equal (see tests).

### 4.3 Delta-vs-snapshot replaces `write_non_tracked_only` special cases

Today the tracked-vs-untracked write decision is inconsistent: `prepare_optim` writes
tracked files for the tracing step (upgrading a base snapshot), while `prepare_mt`
passes `write_non_tracked_only=True` for the same step (the snapshot already carries
tracing). With per-feature flags this generalizes cleanly:

- The features applied to a workspace are recorded in a **git-tracked metadata file
  in the workspace itself** (4.4), so every snapshot carries its own prepare record.
- When a run starts from a snapshot, the interpreter restores the snapshot, reads the
  metadata file, and computes the delta: **newly enabled feature** (requested but not
  recorded in the source) => apply fully, including tracked files;
  **already-present feature** => refresh untracked/read-only support files only.
- Fresh workspace (no start snapshot): all requested features apply fully.
- Disabling a feature the source snapshot has (e.g. requesting `tracing=False` on a
  traced snapshot) is not supported today and raises with a clear message; features
  are additive along a chain.

This reproduces the current behavior of all four legacy functions exactly and removes
the per-function `write_non_tracked_only` reasoning.

Snapshots without the metadata file (i.e. produced before this change) are not
supported: the interpreter raises with a clear error. There is no fallback parameter;
re-run the producing stage to obtain a stamped snapshot.

### 4.4 Workspace prepare metadata file (git-tracked)

New file written into the workspace root by the prepare interpreter, e.g.
`.synnodb_prepare.json`:

```json
{
  "format_version": 1,
  "features": { "scaffold": "full", "parallel_ready_impl": true,
                "tracing": true, "mt_helpers": false, "sample_trace": false },
  "parallelism": false
}
```

- **Git-tracked, part of every snapshot.** The file is deliberately *not* on the
  untracked/exclude lists, so it is committed with the first prepare snapshot and
  travels through the whole chain. Restoring any snapshot therefore restores the
  authoritative record of what its files were prepared with - the workspace, not the
  artifact or W&B, is the single source of truth for the delta logic in 4.3.
- **Written by the interpreter only, after all features applied, before the initial
  snapshot commit.** `parallel_ready_impl="auto"` is resolved to its concrete value
  before writing, so the record states what actually happened. `parallelism` records
  the producing run's serving parallelism (known at run start from the plan), which is
  what `checkSfCorrectness` replay needs on the W&B-free path.
- **Deterministic serialization**: sorted keys, no timestamps, trailing newline, so an
  identical feature set always produces identical bytes. The file is tracked, so its
  content feeds the snapshot hash and thus every snapshot-keyed cache - any
  nondeterminism would poison cache stability.
- **Read-only for the agent**: added to `readonly_files_git_tracked`
  (`PrepareWorkspace._get_readonly_files`), so the editor and shell tools refuse to
  modify it while git keeps tracking it. It must also not match any `extra_gitignore`
  pattern in `main()`.
- `storage_plan_text` is not recorded (the injected `storage_plan.txt` is itself a
  tracked workspace file); `sample_trace` is.

### 4.5 Artifact stamping and checkSfCorrectness replay

- `StageArtifact` gains `prepare_features: PrepareFeatures` and `parallelism: bool`,
  populated by the result builder **from the workspace metadata file** and logged
  into the W&B config - a convenience mirror of the file, not a second source of
  truth.
- `prepare_replay_source_run` is deleted. `checkSfCorrectness` restores the source
  snapshot, reads `.synnodb_prepare.json`, and reuses the recorded features as its own
  `PrepareFeatures`; the `needs_parallelism` resolution in `run_conv_wrapper` reads
  the recorded `parallelism` flag. The `source_stage` parameter and every
  stage-name-based resolution path are deleted outright.

## Phase 5: `ConversationPlan` + `SynnoDB.run_synthesis()`

New public surface (defined in `api.py` or a new `plan.py`, re-exported from the
package root):

```python
class SupervisionPolicy(Enum): OFF, STRICT, RELAXED

@dataclass(frozen=True)
class ConversationPlan:
    name: str                                             # identity: conv_name, stage_name tag, debug logs
    prepare: PrepareFeatures
    stages: Callable[[ConvContext], list[StageItem]]
    parallelism: bool = False
    supervision: SupervisionPolicy = SupervisionPolicy.OFF
    finish_interactive: bool = False
    result: ResultBuilder = _build_artifact
```

- `ConvContext` is a curated, documented view over today's `FrameworkContext`:
  `query_ids`, `filenames` (typed, replacing the dict), `workspace_path`,
  `db_storage`, `threads`, `model`, `run_tool`, `workload_provider`, `sql_dict`, plus
  helpers such as `ctx.reference_plans(source="umbra")` (wrapping the reference-plan
  bootstrapping currently in `OptimizationConversation.__init__`, evaluated lazily).
  `FrameworkContext` stays internal.
- **`run_synthesis` is the only entry point.** The `Stage` descriptor and the
  `SynnoDB.run(stage_name)` dispatch are deleted. The registry becomes a plain
  name -> `ConversationPlan` map (needed only by the `manual` CLI); execution always
  flows through
  `run_synthesis(plan, start=...)` -> `run_conv_wrapper` -> engine, for built-ins and
  custom runs alike. There is exactly one code path to reason about, cache, and test.
- **The built-in methods become thin wrappers over `run_synthesis`.**
  `createBaseImpl`, `runOptimLoop`, etc. keep their signatures and chaining ergonomics
  but internally only (a) resolve their chain inputs (artifact / snapshot hash / W&B
  run id, via today's `resolve_source_snapshot` logic, which moves out of the per-stage
  `build_config`s into a shared input-resolution helper), then (b) construct their
  parameterized plan and call
  `run_synthesis(<predefined plan>, start=<resolved snapshot>)`. The
  per-stage `_config_*` functions in `stages.py` dissolve into these wrappers; what
  they contributed to `RunConfig` beyond chaining (fixed flags like
  `use_supervision_agent`, `keep_csv`) moves onto the predefined plans.
- `SynnoDB.run_synthesis(plan: ConversationPlan, *, start=None)`:
  - Takes a complete `ConversationPlan` and executes it. **No kwargs assembly, no
    field overriding**: the plan is the single, self-contained description of the
    run. Variations are expressed by constructing a different plan
    (`dataclasses.replace(plan, name=...)` or a plan factory such as
    `check_sf_plan(target_sf=100)`), never by call-site overrides - so a plan value
    always means the same run, wherever it appears.
  - `start` is the only per-invocation argument: the chain token (artifact ->
    `.snapshot_hash`, raw hash, or None -> fresh workspace) identifies *which*
    snapshot this execution starts from and is deliberately not part of the reusable
    plan. One generic resolver handles it for every caller, replacing the five
    per-stage copies of that logic.
  - Per-call inputs of built-in stages (e.g. `target_sf`, the storage-plan text) are
    baked in when the wrapper constructs its parameterized plan, not passed through
    `run_synthesis`.
  - Validates `plan.name` (identifier-ish; feeds `generate_conv_name`, log files, the
    DuckDB drain, and the W&B `stage_name` tag).
  - Returns a `StageArtifact` stamped with `prepare_features_json` + `parallelism`, so
    custom runs chain into `checkSfCorrectness` or further custom runs with no extra
    ceremony.
- `main()` cleanup limited to what the plan requires: `spec.prepare/factory/...` reads
  become plan reads; `gen_incorrect_output_prompt_fn` and the
  `conv_args`/`auto_conversation_args` dict-splatting collapse into a typed engine
  constructor. Full decomposition of `main()` is worthwhile but explicitly out of
  scope here.

## Phase 6: collapse the class hierarchy

Only now, with equivalence proven by the golden test:

- Move the five built-in stage lists into plain builder functions under
  `src/synnodb/conversations/builders/` (`storage_plan.py`, `base_impl.py`, `optim.py`
  with the in-mem/SSD variants as a `db_storage` branch, `mt.py`, `check_sf.py`). Each
  is `def build(ctx) -> list[StageItem]` plus its private prompt-closure helpers;
  `OptimizeBuildStage` / `ValidateAndFixStage` move alongside.
- Merge `AbstractConversation` + `CheckpointedConversation` into one `Conversation`
  engine module (`conversation_engine.py`). The interactive u/r/i/c machinery becomes
  a private collaborator of the engine, not a base class.
- Delete: `gen_storage_plan_conversation.py`, `base_impl_conversation.py` (class
  part), `optimization_conversation.py`, `in_mem_1_optim_conv.py`,
  `in_mem_2_mt_conv.py`, `ssd_1_st_opt_conv.py`, `ssd_2_mt_conv.py`,
  `check_sf_correctness_conv.py`, and the four legacy prepare
  functions.
- `ScriptedConversation` + the `manual` CLI: re-express scripted as a plan whose
  builder turns the JSON prompt array into `PromptStage`s; `manual_specs.py`
  resolves plans by name. Delete `scripted_conversation.py`.
- Update `tutorials/tpch_byo.ipynb` and the README with a "define your own
  conversation" section - the payoff artifact, and writing it is the usability test
  for the API.

## Test plan per phase

Run with `.venv/bin/python -m pytest` throughout.

| Phase | New/updated tests |
|---|---|
| 0 | Golden stage-list/prompt fixtures for all 7 classes |
| 1 | Marker lowering round-trip (typed item -> legacy string -> conversation JSON identical to before) |
| 3 | SDK branch-semantics pinning test (`get_conversation_turns` vs `create_branch_from_turn`: exactly which turn is excluded from a branch) - written **before** the loop is built; `PerQueryLoop` unit test with mocked SDK wrapper: branch-switch call sequence, anchor emitted iff required (make-mt: yes, optim round 1: no; anchor text byte-identical to today's), measurement order, revert path; `benchmark_sf` on an item is applied before and restored after execution, including on exception |
| 4 | `PrepareFeatures` JSON round-trip; interpreter output byte-equal to each legacy `prepare_*` artifacts string for all four stages x {fresh, from-snapshot}; delta logic (newly-enabled vs already-present) reproduces `prepare_optim` vs `prepare_mt` tracked/untracked behavior; disabling a recorded feature raises; `.synnodb_prepare.json` is deterministic (identical features => identical bytes), git-tracked, survives a snapshot restore round-trip, and is rejected by the editor/shell tools (read-only); checkSf replay resolves features + parallelism from the restored metadata file without `source_stage` |
| 5 | `run_synthesis` end-to-end with a trivial 1-stage plan against the cached/test harness; chain-token resolution matrix (artifact / hash / None / both -> error); every built-in method (`createStoragePlan` ... `checkSfCorrectness`) resolves to a `run_synthesis` call with its predefined plan (assert via spy - no second dispatch path) |
| 6 | Golden test runs against the builders; delete class-specific tests |

## Risks and mitigations

- **Cache invalidation** (LLM, compile, validate caches): prompts and the prepare
  artifacts string are cache keys. Mitigated by the golden test and the
  interpreter-equality test; any fixture diff in review is a blocker.
- **Marker lowering keeps the wire format stable**: typed markers lower to the
  existing strings, so `handle_prompt`, the conversation JSON, and the supervision
  skip set need no changes - a churn-avoidance choice, not a compatibility promise.
- **Old snapshots and W&B runs become non-chainable** (deliberate, no backward
  compatibility): anything produced before Phase 4 lacks the metadata file and is
  rejected with a clear error. Re-run the producing stage where an old chain matters.
- **Feature-delta correctness** (Phase 4.3) is the subtlest new logic: whether a
  feature writes tracked files now depends on the metadata file read from the
  restored snapshot. The dedicated tracked/untracked equivalence tests cover all four
  legacy paths.
- **One-time cache invalidation from the metadata file** (Phase 4.4): adding a
  git-tracked file changes the snapshot hashes of all newly produced workspaces, so
  snapshot-keyed caches (compile, validate, LLM) miss once per configuration after
  the rollout. Existing snapshots and their cache entries stay valid. This is a
  deliberate, one-time cost; it must land together with the rest of Phase 4 (not
  separately) so caches are invalidated only once.
- **Stage-number renumbering** (Phase 3) changes dashboard/debug-log labels across
  versions; acceptable, call it out in the changelog.
- **Biggest single step is Phase 3** (`PerQueryLoop`). If it stalls, everything before
  it still landed value, and the loop-free stages (storage plan, base impl, check-sf)
  can migrate to Phases 5/6 first.

## Sequencing

Phases 0-1-2 are small and mechanical (can land together). Phases 3 and 4 are the two
substantial, independent work packages and can proceed in parallel. Phase 5 depends on
4; Phase 6 depends on all.
