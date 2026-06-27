# Caught Errors in Generation — failure modes & general fixes

> A living catalog of failures hit while generating bespoke engines with the agent
> factory (`createStoragePlan` / `createBaseImpl` / …). Each entry is **named**, with
> its symptom, root cause, and a **general fix** that holds for *any* workload and
> *any* setup. New failures get appended here with the same shape.

## The guiding principle

**Determinism belongs to the framework; creativity belongs to the LLM.**

Every failure below traces to the same mistake: the LLM was made responsible for
something that has *one correct answer* and can be produced by running a command or
calling a tested utility — data provisioning, type-correct decoding, schema
introspection, validation diagnosis, environment checks, run hygiene. The LLM then
gets it subtly wrong (or gets *stuck*), and the whole expensive run is wasted.

The fix is structural, not per-bug: **move the deterministic work out of the LLM.**
We can run commands ourselves. The LLM should be reserved for the genuinely
open-ended decisions — the storage *layout strategy* and the query *execution
algorithm* — and hand everything else a correct, tested primitive to call. Every time
we catch the LLM hand-writing deterministic-but-error-prone code (raw Arrow byte
decoding, endianness, scale factors, paths), that is a signal to promote it to a
framework primitive.

A blunt heuristic for triage: *"Could a 5-line script or a unit-tested helper have
made this impossible?"* If yes, it's a framework gap, not an LLM failure — fix the
framework.

**Corollary — completeness by delegation, not enumeration.** When you move a job into
the framework, the framework's version must be *complete*, or it's just a painkiller
that relocates the same bug and re-bites on the next workload. A helper that handles
"the types I thought of" is **not** a fix — it's the original failure with a new
owner. Real fixes get completeness by **delegating to a primitive that already covers
100% of cases** (Arrow's `compute::Cast`, DuckDB's type system, the OS), then handling
exactly one canonical output — and **failing loudly** on anything the primitive can't
do (never silently). If your fix contains a growing `switch`/`if-elif` over a workload
dimension (types, dialects, encodings), it is incomplete by construction; replace the
switch with delegation.

---

## Catalog

### G1 — Incomplete dataset provisioning
- **Symptom:** validation crashes with `MemoryMappedFile::Open(...): Failed to open
  local file '.../sf1/customer.parquet': No such file`; the run aborts (the tool
  error propagates as a fatal `UserError`).
- **Root cause:** validation exercises *multiple* scale factors
  (`fast_check`=SF{1,2}, `exhaustive`=SF{1,2,benchmark_sf}, `ingest`=SF{benchmark_sf}),
  but only the target SF's parquet existed. The required datasets were never checked
  for, and the LLM cannot conjure missing data.
- **General fix (deterministic, framework):** a **dataset pre-flight** that runs before
  any generation. Derive the *required* `(benchmark, scale_factors, tables)` set from
  the run modes the conversation will use, then for each missing
  `base_parquet_dir/sf<N>/<table>.parquet` **generate it ourselves** (DuckDB
  `CALL dbgen(sf=N)` → `COPY ... TO parquet`, or the workload's generator) — no LLM
  involved. If generation is impossible (unknown benchmark, no disk), fail *fast and
  early* with the exact missing paths, not 40 turns into a run. Also assert the
  router's source-of-truth DuckDB holds the same data, so cross-checks are
  apples-to-apples.

### G2 — Hand-written Arrow decode bugs (type / endianness) — *the expensive one*
- **Symptom:** the engine compiles, ingests, and `count(*)` is correct, but **every
  aggregate is 0 (or garbage)**. Validation reports "incorrect"; the agent then hunts
  in the *query* (`__int128` accumulation, `__restrict__`, the output formatter) — the
  wrong place — and burns hundreds of turns.
- **Root cause:** the LLM hand-wrote the Arrow→memory column decode in `db_loader.cpp`
  and got a low-level detail wrong. Concretely: it decoded `DECIMAL(15,2)` columns with
  `arrow::Decimal128::FromBigEndian(GetValue(i), bw)`, but `GetValue(i)` returns bytes
  in **native (little-endian)** order — so the value columns load as junk/zero. (It
  also wrongly assumed "DuckDB stores DECIMAL as INT64".) This is an entire *class*:
  endianness, decimal scale, date epoch units, null bitmaps, chunk offsets, signed vs
  unsigned, validity — all easy to get subtly wrong by hand, all with exactly one
  correct answer.
- **General fix (deterministic, framework) — `column_ingest.hpp`, BUILT & validated.**
  Ship a framework-owned column-ingestion library and forbid the LLM from decoding
  Arrow buffers itself. **Crucially, coverage is by DELEGATION, not enumeration** — a
  hand-written `switch` over Arrow types is the *same* bug class relocated (it breaks
  on the next workload's TIMESTAMP / DICTIONARY / DECIMAL256 / …). Each helper instead
  **casts the source column to ONE canonical type via `arrow::compute::Cast`** — the
  reference converter that already handles *100%* of Arrow types — and then reads only
  that single canonical type:
    - `scaled_int64(table, col, scale)` → cast to `decimal128(38, scale)`, read unscaled
      int64 (exact fixed-point; **overflow throws**, never truncates silently);
    - `as_int64` → cast to `int64`; `as_double` → `float64`; `as_string` → `utf8`
      (densifies dictionaries); `as_date_days` → `date32` (handles timestamps too).
  Adding a new workload type needs **zero** changes here — if Arrow can cast it, we
  handle it; if it can't, the cast **fails loudly**. The LLM decides *layout* and
  *which* helper per column, never the byte decode. Validated against real TPC-H
  parquet vs DuckDB **and** synthetic bool/dictionary/timestamp/decimal/overflow
  (tests/test_column_ingest.py). This deletes G2 for all workloads.
  - **Cheap deterministic backstop:** after `build()`, the framework runs a
    **load self-check** for free — e.g. `SELECT sum(<col>) FROM <table>` in DuckDB vs
    the engine's loaded vector sum for a few columns. A load bug is then caught
    *immediately, attributed to the loader*, before any query work — turning G2 from a
    400-turn mystery into a one-line diagnosis.

### G3 — Misattributed validation failure & phase-lock trap
- **Symptom:** the agent debugs the wrong component and, worse, refuses to fix the real
  culprit because an earlier phase ("the build") was marked *approved* — so it loops
  over zero data indefinitely (observed: turn 428, going in circles, explicitly
  reasoning *"the loader is buggy but I'm told not to modify it"*).
- **Root cause:** (a) validation reports a bare "incorrect" with no structured
  diagnosis, so the agent guesses; (b) the conversation hard-locks the build phase, so
  a build bug discovered during the query phase is unfixable.
- **General fix (deterministic, framework):** on any correctness mismatch, the
  framework computes a **structured diff** — row-count match? per-column match / max
  abs+rel error / NULL deltas — and a **heuristic culprit attribution**, e.g.
  *"row counts match but all aggregate columns are 0 while group keys are correct →
  suspect value-column ingestion in `db_loader.cpp`, not the query."* Hand the agent
  the diagnosis, not a guessing game. And **never hard-lock a phase against the
  evidence**: if the diagnosis points at the build, the current phase is allowed (and
  told) to fix it. A phase's "approval" is provisional until end-to-end correctness
  holds.

### G4 — Non-idempotent re-runs (state/snapshot/run hygiene)
- **Symptoms:** `AssertionError: Snapshot with name "<hash>" already exists` on re-run;
  `wandb ConfigError: Attempted to change value of key "log_run_name"` when two stages
  share one process; per-run engine workspaces accumulating (`q1_run`, `q1c`, `q1m`…).
- **Root causes:** content-addressed prep snapshots already present in the shared cache
  are re-`snapshot()`'d with a uniqueness assertion; wandb allows one run per process;
  no workspace lifecycle.
- **General fix (deterministic, framework):** make runs **idempotent**. The snapshotter
  should **reuse an existing identical snapshot** (no-op) instead of asserting;
  orchestrate stages as **separate processes** (one wandb run each), chaining by
  `run_id`; provide **ephemeral/auto-cleanup workspaces**
  (`SynnoDB(..., cleanup_workspace=True)` / context-manager / `db.cleanup()` — added)
  and a **fresh-workspace + `disable_repo_sync`** path for self-contained local runs.
  Re-running the same config must Just Work.

### G5 — Silent stub / incomplete phase
- **Symptom:** a phase "succeeds" and is chained onward, but a file is still the
  **template stub** — `run_q1` returns `value1/value2`, `db_loader.cpp` is the 11-line
  TODO. (Our first run shipped a stub query; only our runtime cross-check caught it.)
- **Root cause:** phase completion was judged by "the tool didn't error", not by "the
  generated code is real".
- **General fix (deterministic, framework):** a **stub/sentinel gate** — after each
  phase, scan the files it owns for template markers (`TODO: implement`, the literal
  `value1`/`value2`, the template's known sentinels) and **fail the phase if any
  remain**, instead of proceeding. Cheap, deterministic, catches an incomplete engine
  immediately.

### G6 — Edit-tool corruption (mangled diffs)
- **Symptom:** the LLM's `apply_patch` V4A diff drops/garbles lines (lines starting
  with `-`/`+`/`--` mis-parsed); it then deletes files to recreate them via heredoc —
  risking the plugin ABI.
- **Root cause:** a fragile diff format + a model that struggles to emit it exactly.
- **General fix (deterministic, framework):** prefer **whole-file/`replace_in_file`**
  edits over positional diffs; **compile-guard each edit** (a phase that no longer
  compiles after an edit is auto-reverted with the error fed back) so a corrupting edit
  can never silently persist; and protect framework-ABI files (already read-only) so a
  recreate can't break the plugin contract.

### G7 — Validation-scope leak (a scoped run validates the whole benchmark)
- **Symptom:** a base impl generated for a *subset* (e.g. only Q1) reports the build
  "incorrect" and the agent goes off **implementing all the other queries** — expanding
  `db_loader` to load every table, rewriting stubs — even though only Q1 was in scope.
  (Observed: Q1 validated *correct, exhaustive, SF20, 8.7× faster*; then the next stage
  ran the run tool over all 22 query ids, the Q2–Q22 stubs failed, and MiniMax started
  building the full TPC-H suite at turn ~188.)
- **Root cause:** the two **post-impl correctness stages** (`base check correctness all`,
  `run all queries and fix any errors`) used prompts that said *"check correctness of
  **all queries** … call the run tool once for **all queries together**"* with **no
  query-id scoping** — while the per-query implementation loop and the stages' own
  `post_stage_validate` gates were correctly scoped to `self.all_query_ids`. The
  framework writes stub `query<N>.cpp` for the *whole* benchmark, so "all queries"
  silently pulled the out-of-scope stubs in and the agent obeyed the prompt.
- **General fix (deterministic, framework) — two layers:**
  1. *Prompt scope.* The correctness prompts now take explicit `query_ids`
     (`base_check_correctness_all_prompt(..., query_ids=self.all_query_ids)` /
     `base_run_all_and_fix_prompt(...)`) and tell the model to run *only* those ids and
     **not run, implement, or modify any other query**. (tests/test_base_correctness_scope.py.)
  2. *Root cause — provider/scaffolding scope.* The prompt fix alone was insufficient:
     `OLAPWorkloadProvider` defaulted `query_ids` to the **whole benchmark**, so the
     scaffolder wrote `query1..22.{cpp,hpp}`, `queries.md`, `query_impl`, `args_parser`
     for all 22, and `run(query_ids=None)` validated all 22. The provider now takes the
     requested subset (`main.py` threads `query_list` in; the provider intersects +
     validates against the workload catalog, raising on unknown ids). A Q1 run scaffolds
     exactly `query1.{cpp,hpp}` + `queries.md`(Q1). (tests/test_workload_scope.py.)
  The deeper rule: a *scope* (which queries this run owns) is deterministic state — it
  must be threaded through *every* prompt, tool call, **and scaffolding step**, never
  re-described in prose as "all", never defaulted to the full benchmark. See
  `.plans/workload_agnostic.md` (this is Phase 0 of making workloads fully data-driven).

---

## What to build (the general fixes as framework components)

Prioritized by leverage (each removes a whole class, for every workload):

1. **Framework column-ingestion library** (`to_scaled_int64`/`to_double`/`to_string`/…),
   unit-tested against real parquet; LLM composes, never byte-decodes. *(kills G2)*
2. **Dataset pre-flight** `ensure_datasets(benchmark, sfs, tables)` that generates any
   missing SF parquet itself before a run. *(kills G1)*
3. **Structured validation diagnosis + culprit attribution** + **phase-unlock** when the
   evidence points at an earlier phase. *(kills G3)*
4. **Load self-check** (DuckDB column-sum vs engine vector-sum) right after `build()`.
   *(turns G2 into an instant diagnosis)*
5. **Stub/sentinel completion gate** per phase. *(kills G5)*
6. **Idempotent run orchestration**: snapshot-reuse, per-stage processes, ephemeral
   workspaces. *(kills G4)*
7. **Compile-guarded edits** / robust whole-file edits. *(mitigates G6)*

The throughline: each of these is the framework *running a command or a tested helper*
so the LLM never has to — and never gets the chance to get it wrong.

---

_First written: 2026-06-26, after a MiniMax-M3 run stalled (turn 428) on G2+G3 while
generating TPC-H Q1 at SF20. Append new failure modes above this line, same shape._
