# Production hardening plan

> Scope: fix every defect found in the adversarial review of the Arrow-egress / data-plane /
> `optimize_database` work, to a standard where a query is either served **provably bit-identical
> to DuckDB** or **refused/fallen-back with a verbose, actionable message** - never silently wrong.
> Development cost is not a constraint; correctness, loud failure, and end-to-end proof are.

## Status (Critical + High landed)

Foundation + every CRITICAL/HIGH finding is implemented and verified; the egress type/NULL work
(E and the egress half of D) was completed in parallel and is folded in. A later pass hardened the
correctness core itself - see **Round 2: the heart of the framework** at the end of this document.
Full suite now: **461 passed, 6 skipped (sandbox)**.

- **Foundation** - `synnodb/errors.py` (typed, verbose hierarchy) + nullability plumbing.
- **C1 (CRITICAL)** - shm plane gated by `_shm_schema_ok` *before* ingest; refuses an engine with
  empty `expected_tables`; `optimize_database` refuses to publish when a referenced table is missing.
- **D (HIGH)** - full nullable support: `column_ingest` captures a validity mask (symmetric with
  egress, backward compatible), the generation contract documents SQL null semantics, the
  cross-check treats NULL as distinct from any value. Proven by C++ round-trip + Python tests.
- **E (HIGH)** - egress is type-complete via `arrow::compute::Cast` and fails loud; a bind-time
  guard refuses the genuinely unreachable output types (nested/blob/interval/time) with a verbose
  reason instead of failing inside the engine.
- **PE3 (HIGH)** - `__del__` / context-manager lifecycle; a dropped engine no longer leaks shm.
- **H1/H2 (HIGH)** - publish is now a crash-atomic, lock-serialized symlink flip onto immutable
  versioned dirs under `.versions`.
- **H3 (HIGH)** - engines are refcounted across cursors; the last handle to close releases them.
- **H4 (HIGH/security)** - a relative `parquet_dir` that escapes the engine package is refused.

### MEDIUM / LOW landed

The MEDIUM/LOW phase is now complete as well. Full suite: **439 passed, 6 skipped (sandbox), 0
xfailed**; every adversarial test was flipped to a regression test asserting the fixed behavior.

- **B/C** - `make_table` rejects mismatched column lengths; `decimal_column` rejects a value that
  overflows the column's precision (the one builder that bypasses Cast's range check).
- **F** - the cross-check replaces stringify-and-sort with exact-key grouping + maximum bipartite
  float matching, so tolerance-equal rows are never a false mismatch and exact columns stay exact.
- **PE1/PE2** - the legacy CSV cast is gated by a strict numeric grammar (no hex/scientific); 128-bit
  and unsigned-64 types map to `decimal128(38,0)` instead of truncating to int64.
- **PE4** - the shm ingest refuses up front (`EngineResourceError`) when the snapshot will not fit,
  keeping a tmpfs reserve free.
- **PE5** - the orphan sweep keys on PID + process start time, so a recycled PID no longer keeps a
  genuine orphan forever.
- **PE6** - `_read_arrow` owns its buffer (true snapshot); a later run/close cannot corrupt a
  returned Table.
- **PE7/PE8** - a truncated result surfaces an `EngineExecutionError` with the engine's stderr; the
  0-row contract is documented.
- **M1** - the manifest records `source_db`; `optimize_database` refuses to clobber an engine of the
  same name built for a different database (override with `force=True` / `--force`).
- **M2** - discovery identifies a package by `(publish dir, engine_id)`, so a servable package is
  never shadowed by another sharing its content id (and a re-publish is still re-discovered).
- **M3** - `write_parquet`/`write_csv` with no current result raise a clear SynnoDB error.
- **L1** - an engine bound on the mounted-parquet plane is promoted to the shm hot-load once the
  connection's live tables are present and verified.

## 0. Standard we are building to

1. **No silent divergence.** Every path that could return a non-DuckDB answer either (a) is proven
   exact, (b) refuses at bind time with a precise reason, or (c) diverges, reports the offending
   cells, quarantines, and serves DuckDB.
2. **Loud, typed, contextual failure.** No bare `pyarrow`/`duckdb` stack ever reaches the user. Every
   failure names the engine, query, table/column, value, expected-vs-got, and the remedy.
3. **Proven end to end.** Each fix ships with a test that fails before and passes after, and the
   whole set is exercised against a synthetic dataset that contains the cases TPC-H never does
   (nulls, hugeint, boolean, timestamp, decimal256, negatives, empty results).

## 1. Findings inventory (complete, ranked)

Evidence lives in `tests/test_process_engine_adversarial.py` and `tests/test_discovery_adversarial.py`
(written during review) and the C++ harnesses in the scratchpad. Status: all PROVEN.

| ID | Sev | Area | Defect |
|----|-----|------|--------|
| C1 | CRITICAL | discovery | shm plane ingests+serves a schema-incompatible table when `expected_tables` is empty (no schema gate before ingest) -> **wrong data** |
| D  | HIGH | ingest | NULL silently becomes 0/""/epoch on ingest; egress cannot emit NULL; precondition unchecked |
| E  | HIGH | egress | exactness envelope (types) is unguarded: `{decimal128, int64, double, string, date32}` only; HUGEINT/BOOL/TIMESTAMP/DECIMAL256 silently mis-handled |
| PE3| HIGH | runtime | no `__del__`/context-manager: a dropped `ShmHotLoadEngine` leaks `/dev/shm` + warm subprocess |
| H1 | HIGH | publish | named republish not crash-atomic (two `os.replace`); a crash between them deletes the live engine |
| H2 | HIGH | publish | concurrent republish of one name races -> `OSError(Directory not empty)`, leaks staging dirs |
| H3 | HIGH | lifecycle | `parent.close()` closes engines a still-open cursor shares (use-after-free) |
| H4 | HIGH | security | relative `parquet_dir` (`../../etc`) escapes the engine package (path traversal) |
| B  | MED | egress | `make_table` has no column-length guard -> structurally invalid Arrow table built silently |
| F  | MED | cross-check | sort-by-str + tolerant-float comparison can false-mismatch unordered multi-row float results |
| PE1| MED (legacy) | runtime | `_cast` accepts `"0x10"->16`, `"1.5e2"->150.00`, `"inf"`; "exact cast" comment is false (legacy CSV path) |
| PE4| MED-HIGH | runtime | shm ingest holds DuckDB copy + pyarrow Table + tmpfs file = >=2x steady / ~3x peak RAM, unbounded |
| PE5| MED | runtime | orphan sweep keeps leaked shm dirs forever on PID reuse |
| PE6| MED (latent) | runtime | `_read_arrow` returns a live mmap alias, not a snapshot; corrupts if an inode is ever reused |
| M1 | MED | optimize | published name is `synno-<db.stem>` only; two DBs with the same stem silently clobber |
| M2 | MED | discovery | two packages with the same content `engine_id` dedup to one; a servable one can be dropped |
| M3 | MED | api | `write_parquet`/`write_csv` with no current result raise an opaque DuckDB internal error |
| C  | LOW | egress | `decimal_column` has no precision guard; a value in `(10^38, int128_max)` emits out-of-range |
| PE7| LOW | runtime | a truncated `.arrow` (engine crash mid-write) raises an opaque error, losing stderr context |
| PE8| LOW | contract | a 0-row result is returned as an empty table; correct but undocumented |
| L1 | LOW | discovery | data plane is frozen at first scan; a mounted-parquet engine is never promoted to shm |

Confirmed **not** defects (refuted by the review, no action): `_BRACKET.sub("1")` neutralization,
`_quote_ident`/`SELECT *` identifier safety, `_mount_snapshot_views`/`db_io` path quoting, manifest
v1/v2/v3 round-trip, cross-filesystem `os.replace`, empty-string-as-null masking, double-close,
`cursor.close()` not closing shared engines. The decimal egress itself is **proven bit-exact** for
negatives, the 64-bit boundary, and the full +/-(10^38-1) range - that foundation stands.

## 2. Cross-cutting foundation (build first; everything else depends on it)

### 2.1 Error hierarchy - `src/synnodb/errors.py` (new)

```
SynnoError(Exception)                  # base; carries a structured, multi-line message
  SynnoUnsupportedQuery(SynnoError)    # bind/generation guard refused a query (lists every reason)
  EngineExecutionError(SynnoError)     # the engine subprocess failed (engine, query, req_id, stderr)
  EngineDivergedError(SynnoError)      # cross-check mismatch (carries the offending-cell diff)
  EngineResourceError(SynnoError)      # shm/tmpfs/subprocess/io budget problem
```

Each subclass takes structured fields and renders a verbose `__str__`. Example for the guard:

```
SynnoUnsupportedQuery: synno-sales / Q3 cannot be routed - 3 column(s) outside the exact envelope:
  - input  'orders.discount'  is NULLABLE: ingest maps NULL->0, which diverges from SQL
                               (COUNT/AVG/IS NULL/arithmetic). Make it NOT NULL to route.
  - output 'total_rev'         is DECIMAL(40,4): exact egress supports DECIMAL(p<=38). Unsupported.
  - output 'n_orders'          is HUGEINT: would overflow int64 egress.
  Action: this query is served by DuckDB (correct, not accelerated).
```

### 2.2 The exactness-envelope guard (fail fast at bind)

A single function `check_routable(input_cols, output_cols) -> list[Reason]` consulted wherever an
engine is bound or generated:

- **Input** (from `expected_tables` + live `information_schema`, extended to carry **nullability**):
  every column the query reads must be NOT NULL. (Nullable-but-no-actual-nulls is allowed only after
  the ingest-time `null_count` check in 2.3; schema-nullable is a hard refuse at bind for safety,
  with an override flag `allow_nullable=False` default.)
- **Output** (from `describe_output`, which discovery already calls): every column type must be in
  the exact egress set. After Phase 1 that set is
  `{DECIMAL(p<=38, s), BIGINT/INT/SMALLINT/TINYINT, HUGEINT, BOOLEAN, DOUBLE/FLOAT, VARCHAR, DATE, TIMESTAMP}`;
  anything else (DECIMAL256, DECIMAL(p>38), INTERVAL, LIST/STRUCT/MAP, ...) refuses.

Call sites: `router/registration.build_binding` (so an unroutable query never registers and
`why()` reports the reason), and `workloads/engine_publish`/`optimize_database` (so generation/bind
refuses early). The verdict is surfaced through the existing `why()` API.

### 2.3 Verbose runtime failures

- **C++ side** - a tiny `cpp_helpers/synno_error.hpp` formats `key=value` context. `column_ingest`
  overflow, `column_egress` length/precision violations, and `result_writer` IO failures all throw
  with `engine/table/column/row/value/expected/got`. `query_impl.cpp` catches and reports via the
  existing per-query error channel.
- **Python side** - `ProcessEngine.run` wraps result reading; any failure raises
  `EngineExecutionError` carrying `engine_id, query_id, req_id, response, stderr[-2000:]` instead of a
  raw pyarrow error (fixes PE7).
- **Ingest null guard** - `ShmHotLoadEngine.ingest` checks each Arrow column's `null_count`; >0 raises
  `SynnoUnsupportedQuery` naming the column (precise: only fires when real nulls exist) (fixes D at
  runtime; the bind guard is the fail-fast layer).
- **Cross-check diff** - on mismatch the router builds and logs an `EngineDivergedError` naming the
  first K differing cells (row, column, engine, duckdb), then quarantines + serves DuckDB.

## 3. Phased remediation

### Phase 0 - Safety net & test normalization
- Add `errors.py`, `check_routable`, `synno_error.hpp`, and the `why()` verdict plumbing (2.1-2.3).
- Extend `ColumnSpec`/`expected_tables` and the optimize/registration paths to carry **nullability**.
- Normalize the two adversarial test files: convert each `*_BUG_*` (asserts buggy behavior) into a
  regression test asserting the **fixed** behavior, gated to the phase that fixes it; keep the
  `*_GUARD_*` tests. Until a fix lands, mark its test `xfail(strict=True, reason=<ID>)` so the suite
  is green and a fix that lands flips it to a hard failure demanding the assertion be updated.

### Phase 1 - Correctness
- **C1**: in `_bind_engine`, gate the shm plane *before* `ingest()` - require non-empty
  `expected_tables` for any `shm_capable` engine and fingerprint live columns against the manifest;
  mismatch -> skip (no ingest, no shm write). `optimize_database` must **refuse to publish** if any
  referenced table is missing (today it warns and ships a partial engine that triggers C1).
- **D**: bind guard rejects nullable input columns; `ingest` rejects real nulls via `null_count`
  (2.2/2.3). Optionally add nullable egress later, but refuse-and-fallback is the correct default.
- **E + PE2**: extend `column_egress` with `int128_column`(->`decimal128(38,0)` for HUGEINT),
  `bool_column`, `timestamp_column`; extend `_arrow_type_for` to match (HUGEINT->`decimal128(38,0)`,
  TIMESTAMP->`timestamp(us)`, BOOLEAN->`bool`). Anything still outside the set is refused by the guard.
- **B**: `make_table` validates equal column lengths and throws `egress_error` listing the offending
  column and the two lengths; debug builds also `ValidateFull()`.
- **C**: `decimal_column` checks `abs(v) < 10^precision` per value and throws with the value/precision.
- **PE1**: harden `_cast` with a strict numeric grammar pre-check (reject hex/scientific/inf/`nan` for
  integer targets; reject non-plain forms DuckDB would never print) before trusting the Arrow cast;
  fix the false comment. (Legacy CSV path; Arrow egress is unaffected.)

### Phase 2 - Runtime & resource hardening
- **PE3**: add `__del__ -> close()` and `__enter__/__exit__` to `ProcessEngine`/`ShmHotLoadEngine`;
  idempotent close.
- **PE4**: stream each table to `/dev/shm` (write batches, drop the intermediate pyarrow Table); before
  ingest, check free tmpfs vs the dataset size and raise `EngineResourceError` if it will not fit;
  document the >=2x steady-state cost and point at the parquet plane as the low-RAM alternative.
- **PE5**: tag ingest dirs with `boot_id` + creation time; reap on (dead PID) **or** (age threshold),
  not PID-alive alone; ignore dirs from other boots.
- **PE6**: own the result buffer - read the `.arrow` into owned memory (not a held mmap) so the
  returned Table is a true snapshot; results are small (aggregations), so the copy is negligible. If a
  large-result/ETL zero-copy path is ever needed, gate it behind a documented unique-inode contract.
- **PE7/PE8**: opaque-partial-arrow -> `EngineExecutionError` with stderr (2.3); document the 0-row
  contract and add a test.

### Phase 3 - Publish & discovery robustness
- **H1+H2**: replace the two-`os.replace` swap with **versioned dirs + atomic symlink flip**:
  publish into `engines/.versions/<engine_id-or-uuid>/`, then atomically repoint the `engines/<name>`
  symlink via `os.replace(tmp_symlink, name_symlink)` (crash-atomic and concurrency-safe; last writer
  wins cleanly). Discovery resolves symlinks; an unreferenced version is GC'd on the next publish. Add
  an `flock` per name as belt-and-suspenders; sweep stale `.tmp-*`/`.old-*`/orphan versions on publish.
- **H3**: refcount engines across connections sharing a router - each `SynnoConnection` (parent or
  cursor) increments on create / decrements on close; engines close only when the count reaches 0.
- **H4**: `_resolve_parquet_dir` enforces containment - a relative `parquet_dir` must resolve under
  `engine_dir` (reject any `..`/absolute escape) with a clear `SynnoError`; migrate the factory to
  relative bundled paths.
- **M1**: default name stays `synno-<stem>` but publishing refuses to replace an existing
  `synno-<name>` whose `expected_tables`/source differ, unless `--force`/`name=` is given (clear error
  naming the conflict).
- **M2**: key discovery identity on the **publish directory**, not `engine_id` alone; when two packages
  serve the same normalized SQL, bind the one servable for this connection (prefer live-data shm, else
  bundled snapshot) and log the tie.
- **M3**: `write_parquet`/`write_csv` raise a clear `SynnoError("no result to write; call execute()
  first")` when `_current is None`.
- **L1**: allow a re-bind when a faster plane becomes available (unregister + re-bind on shm once live
  tables are present), instead of skipping by id.

### Phase 4 - Cross-check correctness
- **F**: replace sort-by-str + positional tolerant compare with **exact-key grouping + per-group
  tolerant matching**: partition columns into exact (Decimal/int/date/string) and float; group both
  sides by the exact tuple; per-key counts must match; within a group, compare the float tuples with
  tolerance via bipartite (Hungarian) matching for small groups, sorted-pair for large. Removes the
  false-mismatch while staying exact on the exact columns. Apply to both `adapt.results_equal` and the
  generation validator so they share one comparator.

### Phase 5 - End-to-end verification (see Section 4)

## 4. End-to-end test strategy

TPC-H exercises none of the dangerous cases, so we build a dedicated fixture.

**Synthetic edge dataset** (`tests/fixtures/edge_db.py`): a small DuckDB `.db` with tables that carry
a nullable integer (with real NULLs), a HUGEINT, a BOOLEAN, a TIMESTAMP, a DECIMAL256, a negative
DECIMAL, a string with quotes, and tables sized to yield empty and 0-row results.

**Test matrix** (each row is an E2E or harness test; "verbose" = asserts the exact message text):

| Finding | Test | Drives | Asserts |
|---------|------|--------|---------|
| guard/E/D | `test_guard_refuses_unsupported.py` | `optimize_database` + `connect` over edge_db, no compiled engine needed | `why()` returns the verbose refusal; query falls back; DuckDB-correct |
| C1 | `test_shm_schema_gate.py` | shm_capable manifest, empty `expected_tables`, wrong live schema | engine **not** registered; no `/dev/shm` write; falls back |
| D (runtime) | `test_null_fallback_e2e.py` | q1q6byo Q6 bound to null-injected lineitem | divergence reported with cell diff; quarantined; DuckDB served |
| E egress | C++ harness `egress_types_test.cpp` | int128/bool/timestamp columns | round-trip bit-exact vs DuckDB Arrow |
| B/C | C++ harness (extends the decimal harness) | mismatched length / out-of-range | throws verbose `egress_error` |
| PE1 | `test_process_engine.py` (extend) | hex/scientific/inf CSV cells | stay strings -> cross-check catches |
| PE3/PE4/PE5/PE6 | `test_process_engine_lifecycle.py` | ingest/drop/GC, tmpfs budget, sweep, snapshot | no leak; budget error; snapshot stable |
| H1/H2 | `test_publish_atomicity.py` | crash between steps; concurrent republish | engine always present; no `OSError`; no leak |
| H3 | `test_connection_lifecycle.py` | parent.close() with open cursor | cursor's engines stay live until last close |
| H4 | `test_parquet_dir_traversal.py` | manifest `parquet_dir="../x"` | refused with `SynnoError` |
| M1/M2/M3/L1 | `test_discovery_adversarial.py` (flip BUG->regression) | the proven repros | fixed behavior |
| F | `test_cross_check_matching.py` | unordered multi-row float results | no false-mismatch; real diffs still caught |
| happy path | `test_shm_hot_load.py` (existing) | q1q6byo Q1/Q6 both planes | still bit-exact, 0 mismatches |

Some new-type egress E2E needs a compiled engine emitting that type; where the factory is too heavy,
prove egress with a C++ harness (as done for decimals) plus a hand-written fixture engine for one
representative query.

## 5. Verification gates

- **G0**: `errors.py` + guard + verbose plumbing land; `why()` shows verdicts; suite green.
- **G1**: Phase 1 - guard refuses every out-of-envelope case in the matrix; C1 closed; new egress
  types round-trip bit-exact in the harness.
- **G2**: Phase 2 - no shm/subprocess leak under drop/GC; tmpfs budget enforced; snapshot stable.
- **G3**: Phase 3 - publish atomic under crash+concurrency; no traversal; lifecycle correct.
- **G4**: Phase 4 - cross-check has no false-mismatch and still catches real diffs.
- **G5**: full `.venv/bin/python -m pytest` green (incl. the flipped adversarial tests and the new
  E2E matrix); `test_shm_hot_load.py` still 0 mismatches both planes.

## 6. Test-file disposition (immediate)

The review left `tests/test_process_engine_adversarial.py` (passes; pins current behavior) and
`tests/test_discovery_adversarial.py` (9 fail by design). These are evidence, not final tests. Phase 0
normalizes them: `*_BUG_*` become regression tests asserting fixed behavior (xfail-strict until their
phase lands), `*_GUARD_*` stay. The suite is intentionally red on the discovery file until Phase 0.

---

## Round 2: the heart of the framework (cross-check / routing core)

A second adversarial pass, this time on the correctness core itself - the route/verify pipeline in
`router.py` and the result comparison in `adapt.py`. Four findings; all reproduced end-to-end
before any change (`scratchpad/repro_heart.py`), all fixed, all regression-tested.

### H-A (CRITICAL, fail-open): the cross-check could serve a known-wrong result
The cross-check computed the trusted DuckDB reference, then on ANY exception in the comparison
served the **engine** result unverified - even though the correct reference was already in hand. An
existing test even asserted `rows == [(999,)]` (the wrong engine answer) when DuckDB had returned
`4`. This is the same class as the earlier incomplete-egress fail-open: a wrong answer shipped while
the right one sat in a variable.

Fix (fail-closed): split reference execution from comparison.
- reference execution fails -> **fall back** so the caller runs DuckDB and surfaces the real error
  (or recovers), exactly as an un-routed query would. Never serve the engine result unverified.
- comparison fails (reference in hand) -> serve the **verified DuckDB reference**, and charge an
  engine failure so a persistently un-comparable engine trips the breaker.
- divergence -> quarantine + serve the reference (unchanged), now via the same honest path.

### H-A2 (HIGH, dishonest UI/metrics): a diverged engine was reported as the server
On a divergence (or comparison error) we correctly served DuckDB's result, but the connection still
reported `served_by="engine"`; the interactive footer would print "synno engine ... 8.4x vs DuckDB"
for an engine that just **failed** its cross-check, and the routed counter was inflated. Added
`RouteTrace.served_by`; the router sets it `duckdb` whenever the reference is served, `connection`
reads it for the footer, and `_tally` records the cross-check outcome instead of a routed serve.
The footer after a divergence now reads `DuckDB`, honestly.

### H-C (HIGH, false-quarantine): ORDER BY with ties sidelined correct engines
A top-level `ORDER BY` on a non-unique key was compared strictly position-by-position, so a correct
engine that broke a tie differently from DuckDB (a freedom SQL explicitly grants) failed the
cross-check and was **quarantined on its first query** - losing all acceleration for the session.
Fix: `normalize.order_by_key_indices` resolves the ORDER BY keys to output-column indices (name,
alias, or ordinal; `None` if any key is an expression / not projected), and `results_equal` does a
**tie-aware** comparison - key columns must match positionally (the real ordering contract) while
tied rows are compared as a multiset. A genuine ordering bug (wrong key sequence) and any data
difference are still caught; when keys are unresolvable it falls back to strict (conservative,
over-rejects only). `results_diff` mirrors this for accurate divergence messages.

### H-B (HIGH, missing safety net): no burn-in, so new engines' early results were unverified
With sampling at the default 10%, a freshly built engine's first queries (and ~90% of all queries)
were served **unverified**; a systematically wrong engine could ship many wrong answers before a
sampled check happened to hit one. Added burn-in: a template's first `verify_first_n` (default 50,
`SYNNODB_VERIFY_FIRST_N`) executions are **always** cross-checked, so a wrong engine is caught and
quarantined on its first queries. `cross_check_rate == 0` remains a total, explicit opt-out and
disables burn-in too.

### Tests (all green)
- `tests/test_cross_check_divergence.py` - the old fail-open test split into two fail-closed tests
  (comparison error serves verified DuckDB; reference error falls back).
- `tests/test_cross_check_ordering.py` - `order_by_key_indices` unit cases; tie-aware
  `results_equal`; E2E that a tie-permuting engine keeps routing AND a genuinely mis-ordered engine
  is still quarantined (soundness).
- `tests/test_burn_in.py` - wrong engine caught on query #1; exactly the first N checked then
  sampling; `rate==0` opts out; env override.

Full suite after Round 2: **461 passed, 6 skipped (sandbox)**.

### Consciously left (fail-safe, not fail-open)
- `_match_float_vecs` uses sorted positional pairing above a 64-row group for multi-float-column
  **unordered** groups; this can over-reject (false-quarantine) but never accept a wrong result. A
  full large-scale exact bipartite matcher (Hopcroft-Karp) is a possible future upgrade.
- `results_equal` compares column **values/positions**, not names; the user-facing column names come
  from the binding's `output_schema`, so engine name drift is not user-visible.
- Burn-in / session counters are intentionally lock-free (advisory), matching the existing router
  design; under heavy multi-cursor concurrency the burn-in boundary is best-effort (sampling still
  applies).
