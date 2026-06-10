## Summary: where most effort is spent

Most effort is being spent on **setup/exploration and recovery from broad, risky edits**, not on the core task of implementing storage/query changes.

Highest-cost buckets across prompts:

1. **Repeated filesystem/codebase exploration**  
   - Especially **Prompt 1, 3, 5, 9, 10**: lots of `find`, `ls`, broad `grep`, repeated `cat` of many files before editing.
   - This is often redundant because the target files are already implied by the prompt (`query10.cpp`, `query11.cpp`, etc., or `query_impl.cpp` for Prompt 9).
   - Example:
     - **Prompt 1** starts with `find`, multiple `ls`, then reads several prior queries and headers before touching 3 target files.
     - **Prompt 9** spends many turns searching for affinity helpers even though the prompt already says they are in `cpu_affinity.hpp` and the target is `query_impl.cpp`.

2. **Patch thrashing / rewriting files after failed partial edits**  
   - Most visible in **Prompt 0** and **Prompt 10**.
   - Example:
     - **Prompt 0** repeatedly patches `query1.cpp`, inspects line ranges, then rewrites the file wholesale.
     - **Prompt 10** edits storage types globally, then updates many dependent queries, then partially reverts, then re-applies a different global type strategy.
   - This inflates turns because edits are not batched and stop conditions are weak.

3. **Redundant compile/check or pseudo-validation loops**  
   - Several prompts include compile checks even though compilation is externally handled and should be minimized.
   - Worse in **Prompt 10**, where compile/run cycles are repeated after small changes or speculative performance tweaks.
   - Also some “validation” is synthetic rather than tool-backed:
     - **Prompt 0/1/6** use Python sanity math and infer success instead of tightly scoped stop criteria tied to actual required outputs.
   - **Prompt 1** also spends many turns investigating Q11 output semantics and PROFILE visibility, despite the task being instrumentation alignment, not deep benchmark harness archaeology.

## Why turn counts are high

### 1) Prompts are underspecified about scope and target files
- **Prompt 0** says “instrument the execution code of all queries” but later says queries **1,2,3**. That ambiguity invites broad exploration.
- **Prompt 10** says “modify any part of the existing codebase” and optimize Q1, with no bounded strategy, target modules, or max experiment budget. This practically invites sprawling search and speculative redesign.

### 2) Prompts do not enforce a plan-first, edit-second workflow
- The agent often starts with “let me thoroughly read all relevant files” and then performs large exploration bursts.
- Without a required short plan naming exact target files and intended changes, it defaults to browsing.

### 3) Prompts lack stop conditions
- For instrumentation prompts, there’s no crisp “once these files compile and trace lines exist, stop.”
- For optimization (**Prompt 10**), there’s no “try at most 1–2 hypotheses; if no improvement, revert and report.”
- That leads to prolonged retries and speculative benchmarking.

### 4) Broad permission causes broad action
- “Align instrumentation with previous queries” caused repeated reading of many old query files in **Prompts 1–7**.
- “You can modify any part of the existing codebase” in **Prompt 10** led to global storage-layout experiments touching many files.

## Highest-impact prompt fixes

### Fix 1: constrain target files explicitly
For instrumentation batches, rewrite like:

> Instrument only `query10.cpp`, `query11.cpp`, and `query12.cpp`. Reuse the tracing API already defined in `trace.hpp`. Do not inspect unrelated files except `db_loader.hpp` or one already-instrumented query if needed for field names/patterns.

This would cut most of the `find/ls/grep/cat` turns in **Prompts 1–7**.

### Fix 2: require a short plan before any tool use
Add:

> Before editing, provide a 3-bullet plan naming the exact files you will read/edit and why. Then execute without broad filesystem exploration.

This would reduce random searching in **Prompts 0, 1, 3, 9, 10**.

### Fix 3: ban broad filesystem search unless blocked
Add:

> Do not use `find`, broad recursive `grep`, or directory-wide `ls` unless a required file is missing or the prompt does not specify the file. Prefer directly opening named targets.

This is especially impactful for **Prompts 1, 5, 9, 10**.

### Fix 4: batch edits, avoid incremental patch thrash
Add:

> Read each target file once, then apply one batched patch per file. Avoid line-by-line patch retries; if patch context fails, rewrite the file in one pass.

Would have reduced the repeated partial patching in **Prompt 0** and **Prompt 10**.

### Fix 5: tighten validation expectations
For instrumentation prompts:

> Validate only by confirming the required `PROFILE`/`COUNT` lines appear for the specified queries under TRACE and that key counters are non-zero where expected. Do not perform extended analytical investigation unless output is missing or clearly inconsistent.

This would avoid over-analysis like **Prompt 1**’s long Q11/queries.txt/parameter investigation.

### Fix 6: explicitly minimize compile/check cycles
Add:

> Compilation is handled externally. Do not run compile after every small edit. Compile at most once after batching all intended changes, and only re-check if a prior edit plausibly introduced a syntax/type issue.

This directly addresses waste in **Prompts 0, 3, 4, 7, 8, 9, 10**.

### Fix 7: bound optimization experiments hard
For **Prompt 10**, rewrite to:

> Optimize Q1 with at most two implementation hypotheses. First inspect only `query1.cpp`, `db_loader.hpp`, and `db_loader.cpp`. Prefer query-local changes over global storage-layout changes unless you can justify a codebase-wide edit in one paragraph first. After each hypothesis, compare against baseline once. If no improvement, revert and stop.

This is the single biggest reduction lever. Prompt 10’s 450 turns were dominated by unconstrained experimentation and global storage-type churn.

## Evidence-based per-prompt highlights

- **Prompt 0 (59 turns):**  
  Time mostly spent on **patch thrash in `query1.cpp`** and repeated local inspection (`grep`, `sed`, `cat`) after patch failures. Root cause: no instruction to batch or rewrite cleanly.

- **Prompt 1 (83 turns):**  
  Time mostly spent on **broad exploration** plus **over-investigation of Q11 validation behavior**. Root cause: “align with previous queries” + no file/scope limits.

- **Prompt 3 (63 turns):**  
  Similar pattern: many reads of impls/examples/headers before edits; then partial patch failure and file rewrites.

- **Prompt 9 (57 turns):**  
  Major waste is **unnecessary discovery** (`find`, `nm`, recursive grep) even though the prompt names `query_impl.cpp` and `cpu_affinity.hpp` and specifies exact functions/core. This prompt was actually well-scoped; the execution strategy was loose.

- **Prompt 10 (450 turns):**  
  By far the largest cost center. Most effort went to:
  - speculative global storage-layout redesign,
  - cascading edits across many query files,
  - repeated compile/run/perf loops,
  - investigating toolchain/build/cache/system behavior,
  - partial revert/re-apply cycles.  
  Root cause: prompt allows changing “any part,” has no bounded experiment count, no target-file guidance, and no stop rule after failed perf gains.

## Best concise prompt template to reduce turns

For future tasks, use something like:

> Target files: `<exact files>`.  
> First output a 3-step plan.  
> Read only those files plus at most one reference implementation and one schema/header file if needed.  
> Do not use broad `find`/recursive `grep` unless a named file is missing.  
> Batch edits into one patch per file.  
> Compile/check at most once after all edits.  
> Stop once acceptance criteria are met; do not do extra exploration or benchmarking.

## Bottom line

The biggest time sink is **not coding itself**; it is:
- **unbounded exploration**, then
- **iterative patch recovery**, then
- **repeated validation/perf retries without strict stop conditions**.

The highest-impact tuning is to make prompts **file-targeted, plan-first, search-minimizing, and experiment-bounded**.