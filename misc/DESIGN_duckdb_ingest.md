# SynnoDB - DuckDB-native ingestion & referential downscaling

> **Status: Steps 1-2 implemented; Step 3 still design.** This scopes the switch from a
> parquet-file data source to a user-provided DuckDB database, and the FK-preserving
> downscaling that replaces the pre-existing small scale-factor tiers.
>
> **Step 2 (§8) has landed** - DuckDB-native is now `sync_from_duckdb`'s default. A tier is a
> `ratio<f>/tier.duckdb` (downscaler `copy_tier_to_duckdb`; the benchmark tier a zero-copy
> symlink to the source), discriminated by a new `DataSource.DUCKDB`
> ([utils.py](../src/synnodb/utils/utils.py)). The DuckDB oracle materializes flat tables from it
> via ATTACH ([duckdb_connection_manager.py](../src/synnodb/observability/benchmark/systems/duckdb_connection_manager.py),
> keyed into the oracle cache in [system_factory_olap.py](../src/synnodb/workloads/system_factory_olap.py)),
> and the candidate engine ingests it over the shm plane -
> [shm_stage.py](../src/synnodb/cpp_runner/shm_stage.py) stages the tier's tables to `/dev/shm`
> and [tools/run.py](../src/synnodb/tools/run.py) sets `SYNNODB_SHM_INGEST` (which reaches the
> in-memory loader via `run_env`, exactly as `STORAGE_DIR` does for SSD; the loader's shm branch
> was already generated). No parquet on disk. In-memory only (SSD rejected with a clear message);
> the parquet fallback (`serve_from="parquet"`) is retained and is the honest cross-check
> ([test_duckdb_native_tiers.py](../tests/test_duckdb_native_tiers.py) asserts the DuckDB oracle
> returns identical rows either way, §9).
>
> **Verification boundary:** every piece is unit-tested except the *end-to-end* engine-over-shm
> synthesis, which needs a real compiled-engine build (LLM + C++ toolchain) to confirm. The shm
> segment format and the `run_env` env delivery both reuse paths proven elsewhere (serving-time
> `ShmHotLoadEngine`; the SSD `STORAGE_DIR` loader), so the risk is contained, but a green
> notebook engine build is the first true validation.
>
> **Step 1 (§8) has landed** - the referential downscaler
> ([duckdb_downscale.py](../src/synnodb/workloads/dataset/custom_scaler/duckdb_downscale.py)),
> the `SynnoDB.sync_from_duckdb` front door ([api.py](../src/synnodb/api.py)) backed by the
> connection-sourced `register_workload_from_duckdb`
> ([byo_workload.py](../src/synnodb/workloads/byo_workload.py)), the `ratio<f>` tier convention
> ([workload_spec.py](../src/synnodb/workloads/workload_spec.py) `tier_dirname`/`find_sf_dir`,
> routed through the provider + DuckDB oracle), the internal parquet fallback, the rewritten
> [tutorial notebook](../tutorials/gen_tpch_demo.ipynb), and the test suite
> ([test_duckdb_downscale.py](../tests/test_duckdb_downscale.py)). Steps 2-3 (DuckDB-native
> synthesis over the shm plane + a `DataSource.DUCKDB` oracle, and removing parquet from the
> SSD/publish corners) remain as designed below.
>
> One deliberate refinement landed vs. §6.3's literal "OR every incident edge": propagation is
> **parent-ward only** (follow edges toward the anchor) and **AND-s multiple edges to the same
> neighbour** (composite relationships), with whole neighbours never used as filters. This is
> what actually delivers the §11 "propagation blow-up" and low-cardinality-edge guards - without
> it a 10% tier of TPC-H comes back at ~99% of full. See the module docstring.
>
> **Feedback welcome inline** - the resolved calls are collected in
> [§10 Resolved decisions](#10-resolved-decisions); leave notes there or against any section.

---

## 1. The paradigm shift

**Today (parquet-rooted):**

```
user generates/downloads parquet  ->  <root>/sf<N>/<table>.parquet
        │                                     │
        │  register_workload_from_json(parquet_dir=...)
        ▼                                     ▼
  synthesis pipeline reads sf1/sf2 (cheap correctness) + sf5 (benchmark) parquet
  runtime: user does CREATE TABLE t AS SELECT * FROM read_parquet(...); engine ingests those tables
```

**Proposed (DuckDB-rooted):**

```
user holds a live DuckDB connection  ──passed to the driver (no copy)──▶  SynnoDB
        │
        │  SynnoDB(duckdb=conn).sync_from_duckdb(queries_json=...)
        ▼
  derive tiers FROM the live connection (never written back to it):
     - benchmark tier  = the full dataset, read zero-copy as-is
     - fast-check tiers = FK-preserving DOWNSCALED samples (fractions), derived, not supplied
        ▼
  synthesis pipeline + oracle consume those tiers - straight from DuckDB, no parquet
  runtime: engine ingests directly from the connection's tables
```

The user no longer supplies pre-scaled parquet tiers. They hand us **one live DuckDB connection**; we
derive everything - schema, tables, the full-scale benchmark data, and the small validation rungs -
from it, without copying it or writing anything back.

---

## 2. Key finding that reframes the scope

**The runtime serving path already ingests directly from DuckDB - not parquet.**

Per [misc/DESIGN_router_dataplane.md](DESIGN_router_dataplane.md) §4.1 and the implementation-status
table, when you `synnodb.connect()` and run a query, the parent process reads the *live DuckDB
tables* as Arrow (`SELECT * FROM <table>`) into a `/dev/shm` directory (`SYNNODB_SHM_INGEST`), and
the generated engine maps each `<table>.arrow` **zero-copy**. Concretely:

- [discovery.py:169-177](../src/synnodb/duckdb_compat/discovery.py#L169-L177) `_aligned_arrow()` does
  `{t: duck.execute(f'SELECT * FROM "{t}"').to_arrow_table() for t in tables}` - the DuckDB→engine
  hand-off already exists.
- [process_engine.py:349-424](../src/synnodb/router/process_engine.py#L349-L424)
  `ShmHotLoadEngine.ingest()` writes those Arrow tables to `/dev/shm` and sets `SYNNODB_SHM_INGEST`.
- The C++ loader's shm branch is emitted at
  [prepare_workspace_olap.py:213-217](../src/synnodb/cpp_runner/prepare_repo/prepare_workspace_olap.py#L213-L217)
  (`ReadArrowTableFromShm`), the parquet branch at the same site's `else`.

So the notebook's `CREATE TABLE t AS SELECT * FROM read_parquet(...)` is only a way to *populate*
the DuckDB the engine ingests from. **The engine already eats from DuckDB at serving time.**

**Consequence:** this is not a data-plane rewrite. It is two localized changes:

1. **Entry / connection** - let the user hand in a live DuckDB connection and have both the factory
   and the runtime source from it directly (instead of hand-loaded parquet tables).
2. **Synthesis pipeline** - the factory needs benchmark + small-validation tiers for the
   candidate-engine build and the DuckDB oracle. These tiers must now be **derived from the
   connection** - the default feeds them straight from DuckDB (shm ingest + a DuckDB oracle, §5.3),
   the internal fallback materializes them to `ratio<N>/<table>.parquet` - and the small tiers must be
   **FK-preserving downscales**.

---

## 3. The one convention everything hangs off

Every data access in the factory funnels through a **parquet root** holding one directory per tier.
Today that directory is `sf<N>/<table>.parquet`; this design **retires the scale-factor concept** for
a **sampling-ratio** one, so the tier directory becomes `ratio<N>/<table>.parquet` (§6, resolved
decision 5). The single resolution primitives:

- [workload_spec.py:98-111](../src/synnodb/workloads/workload_spec.py#L98-L111) `parquet_root()`
- [workload_spec.py:125-139](../src/synnodb/workloads/workload_spec.py#L125-L139) `find_sf_dir(sf)` -
  retargeted to build `ratio<N>` dirs (and renamed accordingly, e.g. `find_ratio_dir(ratio)`).
- `WorkloadSpec.base_parquet_dir` carries the absolute root for bring-your-own workloads.

Tier ladders live on the spec:
[workload_spec.py:113-122](../src/synnodb/workloads/workload_spec.py#L113-L122)
`scale_factors_for(run_mode)` → `fast_check_sfs` / `exhaustive_sfs` / `ingest_sfs` / `benchmark_sf`,
whose values become **sampling ratios** rather than scale factors (the SF-flavored names are renamed to
match).

### 3.1 The change surface (every site that assumes `sf<N>/<table>.parquet` on disk)

| Concern | Site | Assumes |
|---|---|---|
| Path convention | `workload_spec.py:98-139` | `parquet_root()`, `find_sf_dir()` resolve `sf<N>` dirs |
| Per-run path build | `workload_provider_olap.py:316-321` | `base_parquet_dir/f"sf{sf}"` + `/{table}.parquet`; minted into `exec_settings.parquet_dir` + `cli_call_args` |
| RAM preflight | `workload_provider_olap.py:238-243` | `sf_dir/f"{table}.parquet"` exists |
| Engine CLI arg | `workload_provider_olap.py:321` → `run.py:464` | `./db <sf_dir>/` |
| Engine loader (generated C++) | `prepare_workspace_olap.py:_gen_table_reads` (208, 218-221); `templates/parquet_reader.cpp` | `ReadParquetTable(path+"{table}.parquet")` |
| C++ entrypoint | `db_olap.cpp:63,97` | `argv[1]` is a parquet dir |
| **DuckDB oracle** | `duckdb_connection_manager.py:172-175` | `read_parquet('{parquet_path}/sf{sf}/{table}.parquet')` - the *only* oracle read |
| Oracle wiring | `system_factory_olap.py:80-81,95-97` | `parquet_dir.parent` + `sf`; keyed by `DataSource` |
| BYO schema DDL | `byo_workload.py:284-302` | `DESCRIBE ... read_parquet(sf<N>/{t}.parquet)` |
| BYO table infer / SF ladder | `byo_workload.py:207-281` | globs `sf*` dirs - **has the `TODO(byo-sampling)` for downscaled samples** |
| Publish expected_tables | `main.py:713-742` | `sf_dir/f"{t}.parquet"` |

**Design lever:** the rows above funnel through the two resolution primitives, so retiring `sf<N>` for
`ratio<N>` is a **concentrated rename** in `parquet_root()`/`find_sf_dir()` (plus the few sites that
hardcode the `sf` prefix or read `read_parquet('.../sf{sf}/...')`); callers that go through the
primitives are untouched. Once the primitives resolve `ratio<N>`, if the **internal parquet fallback**
materializes real `ratio<N>/<table>.parquet` tiers from the DuckDB, the rest of the factory keeps
working unchanged (§5.2). The exposed default instead *replaces* these sites with DuckDB-native
equivalents (shm ingest + a `DataSource.DUCKDB` oracle, §5.3), so no scale-factor labels or on-disk
parquet remain in the shipped path (resolved decisions 4 & 5). Either way the change collapses into the
directory-convention rename + the *registration/extraction* step + a new downscaler. This is the spine
of the plan (§5).

---

## 4. What already exists that we reuse

- **Read tables straight off a live connection** (the primary model, resolved decision 1): the
  serving path already does `{t: duck.execute(f'SELECT * FROM "{t}"').to_arrow_table() ...}`
  ([discovery.py:169-177](../src/synnodb/duckdb_compat/discovery.py#L169-L177)) - the same hand-off
  `sync_from_duckdb` reads through, with no file attach and no copy. The read-only ATTACH primitive
  ([db_io.py:35-65](../src/synnodb/duckdb_compat/db_io.py#L35-L65) `load_database_into_memory()`:
  `ATTACH '<file>' (READ_ONLY)` → `CREATE TABLE main.t AS SELECT * FROM alias.t` → `DETACH`, leaving
  the user's file untouched) remains for the internal fallback that opens a file on the caller's
  behalf.
- **Bind a `.db` to an engine + publish with provenance**:
  [optimize.py:177-362](../src/synnodb/optimize.py#L177-L362) `optimize_database()` already derives
  `expected_tables` from `information_schema`, cross-checks the engine against the source DB, and
  publishes a manifest with a first-class `source_db` field
  ([manifest.py:112-115](../src/synnodb/router/manifest.py#L112-L115)).
- **`connect()` already accepts a file path** - passed straight to DuckDB
  ([duckdb_compat/__init__.py:74-76](../src/synnodb/duckdb_compat/__init__.py#L74-L76)).
- **Existing DuckDB scaler**:
  [scale_parquet.py](../src/synnodb/workloads/dataset/custom_scaler/scale_parquet.py) - reads a
  DuckDB, has scale-up (row multiplication with PK/FK offsets) and scale-down. **But its scale-down
  samples each table independently by percentage** ([scale_parquet.py:357-444](../src/synnodb/workloads/dataset/custom_scaler/scale_parquet.py#L357-L444)),
  which breaks referential integrity - exactly the gap this doc closes.
- **The `DataSource` enum** (`FLAT`/`PARQUET`/`BESPOKE`,
  [utils.py](../src/synnodb/utils/utils.py)) is already a cache-key dimension for the oracle - the
  natural place to add a DuckDB-attached representation if we ever skip parquet materialization
  (§8, phase 2).

---

## 5. Proposed design

### 5.1 Entry point

The front door is a method on the `SynnoDB` driver ([api.py:282](../src/synnodb/api.py#L282)), not a
file-path helper. The caller owns a **live DuckDB connection** and hands it to the driver; SynnoDB
never opens, copies, or moves a file - it reads the connection's tables directly (the same
`SELECT * FROM <table>` → Arrow hand-off the serving path already uses, §2):

```python
import duckdb, synnodb

conn = duckdb.connect("my.duckdb")            # or an in-memory conn the caller has already populated
db = synnodb.SynnoDB.in_memory(duckdb=conn)   # the driver is handed the live connection - no copy
db.sync_from_duckdb(                           # runs ingest with the connection's data
    name="tpch_byo",
    queries_json="queries.json",
    downscale_tiers=(0.02, 0.1),              # sampling ratios -> fast-check rungs, via referential closure
    join_relationships=None,                  # optional explicit join edges when inference misses (§6)
)
```

`sync_from_duckdb` (a) reads schema + tables + row counts off the connection, (b) infers the join
graph (§6, primarily from `queries.json`), (c) derives the benchmark + downscaled tiers (§5.2) as
**ephemeral DuckDB tables off that connection** - nothing is copied to disk and nothing is written
back to the user's store - and (d) registers a `WorkloadSpec` (via an internal
`register_workload_from_duckdb`, the connection-sourced sibling of `register_workload_from_json`)
whose tier ladder is `{downscale ratios} ∪ {1.0 (full)}`, then sets it as the driver's workload.
Everything downstream is unchanged.

The connection is the **single source of truth and the only representation** - "not copying,
whatsoever." The benchmark tier is the connection's tables read zero-copy; the downscaled tiers are
temp tables derived from it and dropped with the session. A parquet materialize/load path is retained
internally only (§5.2), never the default.

For the **runtime drop-in**, `synnodb.connect("my.duckdb", engines=...)` already opens the file as
the inner DuckDB; the engine's shm plane already ingests its tables. The only wiring gap is
auto-discovery binding when the user opens the connection directly (today the notebook hand-creates
tables first). Small, and mostly done - see §7.

### 5.2 Registration-time extraction: DuckDB-native, with an internal parquet fallback

Registration derives everything from the live connection - table list, per-table schema (`DESCRIBE`),
join graph (§6) - and builds the benchmark + downscaled validation tiers with the downscaler (§6.3).

**DuckDB-native (the path - resolved decision 4).** Tiers stay inside DuckDB and are keyed by their
**sampling ratio**, not a scale-factor label (resolved decision 5). The sampler writes each downscaled tier as
ephemeral DuckDB tables (temp tables / a scratch in-memory DB) derived off the connection; the
benchmark tier is the connection's tables as-is. Synthesis feeds the candidate engine over the **shm
plane** (the Arrow hot-load that already exists for serving, §2) and the DuckDB oracle reads the tier
tables directly. No parquet touches disk. This is the only exposed mode.

**Internal parquet fallback (retained, not exposed).** The same downscaler can `COPY` each tier to
`<managed_root>/ratio<fraction>/<table>.parquet` (e.g. `ratio0.02/`, `ratio1.0/` - the folder names
the sampling ratio, no `sf`), and registration sets `base_parquet_dir=<managed_root>`; the whole
factory + oracle + the retargeted `find_ratio_dir` (§3) then run against the tier dirs unchanged. This
path lives behind an internal flag - not the default, not user-facing - as a fallback and to validate
the sampler against the working factory end-to-end. It is also what `register_workload_from_json` (the
bring-your-own-parquet entry, kept per resolved decision 6) uses.

```
live conn ──read (no copy)──▶  derive schema + join graph (§6)
   │
   ├── benchmark tier   = full dataset (the connection's tables, read zero-copy)
   └── each fraction f  ──▶ largest-table-anchored sample (§6.3)
                 │
                 ├─ DuckDB-native (default): kept sets are ephemeral DuckDB tables ─▶ shm-fed synthesis + attached oracle
                 └─ internal fallback     : COPY kept sets TO <managed_root>/ratio<f>/<table>.parquet
   ▼
 WorkloadSpec(tier ladder = {<f1>, <f2>, …, full},
              dataset_version=<hash of source tables + tier params>)   # cache-busting
```

- One downscaler; only the sink differs (ephemeral DuckDB tables vs. `COPY ... TO parquet`).
- **Idempotent / cached** like `ensure_tpch_parquet`; the `dataset_version` (hash of the source tables
  + tier fractions + downscaler version) feeds the LLM/snapshot cache key so re-extraction invalidates
  stale caches.

### 5.3 What "remove parquet everywhere" requires

The synthesis path is parquet-bound at exactly two points, and each has a DuckDB-native replacement
that **already exists** elsewhere in the codebase - so this is routing, not a new data plane:

| Parquet-bound today | DuckDB-native replacement (already exists) |
|---|---|
| Candidate engine launched `./db <parquet_dir>`; loader calls `ReadParquetTable` (`db_olap.cpp:63,97`; `prepare_workspace_olap.py:218-221`) | Feed the **shm plane** during synthesis - the same `SYNNODB_SHM_INGEST` Arrow hot-load the router uses at serving time (`process_engine.py:349-424`); the loader's shm branch is *already generated* (`prepare_workspace_olap.py:213-217`) |
| DuckDB oracle reads `read_parquet('{path}/sf{sf}/{table}.parquet')` (`duckdb_connection_manager.py:172-175`) | Oracle reads the **attached** tier tables - a new `DataSource.DUCKDB` variant (§8) has it `ATTACH` the tier DB instead of `read_parquet` |

Caveat: the **SSD/persistent** engine template has no shm branch yet (`templates/olap/ssd`), so
DuckDB-native lands **in-memory first**; SSD gets its own non-parquet ingest branch so it too samples
from DuckDB (resolved decision 4), with the internal parquet fallback covering it until that branch
lands (§8 step 3).

---

## 6. FK-preserving downscaling (the core problem)

### 6.1 Why naive per-table sampling is wrong

`scale_parquet.py`'s scale-down samples every table at the same rate, independently. On TPC-H that
means a kept `lineitem` row may reference an `orders` row that was sampled out, and a kept `orders`
row references a `customer` that's gone. The result:

- **Joins collapse.** A 2% independent sample of both `orders` and `lineitem` retains ≈`0.02 * 0.02`
  = 0.04% of the natural `orders⋈lineitem` matches. Most join queries return near-empty.
- **Validation goes vacuous.** The correctness check compares the engine vs DuckDB on the *same*
  downscaled data; if the join yields nothing, a totally broken join still "matches" (both empty).
  The cheap rung stops catching the bugs it exists to catch.
- **Cardinalities/fan-out distort**, so scale-sensitive bugs (hash-table sizing, overflow,
  group counts) don't surface at the small rung either.

The user's phrasing - *"preserve join tuple factors / follow primary/foreign keys"* - is exactly
this: the sample must be **referentially closed** so joins still produce representative rows.

### 6.2 The operating object: join-relationships (not FK constraints)

The downscaler follows **join-relationships** - equi-join edges between two columns,
`table_a.col ↔ table_b.col` - not FK constraints per se. This is deliberate: it is the same object
the workload's queries already express, it is what the closure actually needs, and it frees the
user from declaring FK/PK *direction* (the key side is derived, see below). Declared constraints,
when present, are just one source that we normalize into join-relationships.

Why not lean on declared FKs? I verified empirically (DuckDB 1.5.3):

- **DuckDB's `dbgen` TPC-H declares NO primary/foreign keys** - only `NOT NULL`. `duckdb_constraints()`
  returns zero `FOREIGN KEY`/`PRIMARY KEY` rows for it. And `PRAGMA foreign_key_list` **does not
  exist** in this build (the current `scale_parquet.py` call would throw and silently yield no FKs,
  then fall back to "scale all numeric columns").
- A DB that *does* declare constraints exposes them cleanly via
  `duckdb_constraints()` → `(table, 'FOREIGN KEY', constraint_column_names, referenced_table,
  referenced_column_names)` - trivially normalized to a join edge.

So we cannot rely on declared constraints. The join graph is built **primarily from the queries**,
using any further signal when it happens to be available (resolved decision 2). Three sources, all
producing the same join-edge object:

1. **Inferred from the workload's query JOINs (primary - the one we lead with)** - *the strong,
   elegant signal.* We always have `queries.json`. Every
   `... FROM orders o JOIN customer c ON o_custkey = c_custkey ...` names a real join edge
   `orders.o_custkey ↔ customer.c_custkey`. sqlglot is already a dependency
   ([router/normalize.py]) and extracts equi-join column pairs from the templates. The join graph we
   need to preserve is *precisely the one the queries exercise* - inferring it from the queries is
   both sufficient and targeted, and needs zero user input.
2. **Declared constraints (unioned in when present)** - `duckdb_constraints()`, normalized to join
   edges. Free and exact, but absent on `dbgen` TPC-H, so used as extra signal, never required.
3. **Explicit `join_relationships` (override)** - the caller passes join edges directly, as a list of
   `(table_a.col, table_b.col)` equi-join pairs (the same shape sources 1 and 2 normalize to), for
   anything inference misses. No FK/PK direction required - they state *which columns join*. Keep it
   simple (single-column equi-joins first), matching the project's "reject complex shapes over
   building machinery" stance.

**No edge direction needed.** The sampler (§6.3) only follows join-relationships and keeps *joinable*
rows, so it never has to decide which side is the key - the join-relationship (an unordered column
pair) is all the metadata required. (A uniqueness probe is still used opportunistically to pick which
small tables to keep whole, not to orient edges.)

> **Resolved:** lead with (1) query-join inference, union in (2) declared constraints when present,
> and take (3) explicit `join_relationships` as an override. For the TPC-H demo the join graph is
> recovered from the Q1-Q22 joins with zero user input.

### 6.3 The algorithm: largest-table anchor + join-path propagation

Stated over an **arbitrary** schema - a set of tables with row counts and the join graph `G` from
§6.2 (undirected edges `Tᵢ.col ↔ Tⱼ.col`). Nothing is schema-specific: no table names, keys, or edges
are baked in; the sampler reads them from the connection and the query set.

```
INPUT   tables with row counts;  join graph G (edges = join-relationships);  fraction f
1. ANCHOR = argmax(row count). Sample a fraction f of it, deterministically:
      keep[ANCHOR] = { r ∈ ANCHOR : hash(key(r)) % K < round(f*K) }
   key(r) = the table's declared PK if any, else its join-key column(s), else rowid.
2. PROPAGATE outward over G, breadth-first from ANCHOR. For each table V reached, keep the rows
   joinable to an already-kept neighbour along ANY incident edge whose other end is kept:
      keep[V] = { r ∈ V : ∃ edge (V.vc ↔ U.uc), U already kept, r.vc ∈ π_uc(keep[U]) }
   (incident edges are OR-ed, so a table referenced from two sides is not dropped.)
3. KEEP WHOLE (no sampling): tables below a small-row threshold, and tables with no join path to
   the anchor (disconnected) - cheap to keep, and dropping their rows would only lose coverage.
4. One more PROPAGATE pass to fixpoint closes any edge left dangling by a cycle / cross-edge
   (stop when no keep[·] grows). Sets are already small, so this is cheap.
```

Anchoring on the **largest** table bounds total size (it dominates the row count); "keep joinable
rows" keeps the workload's joins non-empty. Because we only ever *follow* edges and keep matching
rows, **no parent/child direction is needed** (§6.2).

**Generic SQL.** Every table becomes one `CREATE TABLE ... AS SELECT ... WHERE <semi-joins>`, and the
shape is identical for every schema - the anchor is a hash sample, each other table is an OR of
semi-joins to its already-kept neighbours:

```sql
-- anchor A (largest), fraction f, K = 1000
CREATE TABLE keep_A AS
  SELECT * FROM A WHERE hash(key_A) % 1000 < round(f * 1000);

-- every other table V, in BFS order; OR over the edges to already-kept tables U1, U2, ...
CREATE TABLE keep_V AS
  SELECT * FROM V
  WHERE  V.c1 IN (SELECT u1c FROM keep_U1)
      OR V.c2 IN (SELECT u2c FROM keep_U2);
      -- ... a table kept whole (small / disconnected) is just SELECT * with no WHERE
```

These statements are emitted by a driver that is a pure graph walk - nothing schema-specific:

```python
anchor = max(tables, key=row_count)
emit(f"CREATE TABLE keep_{anchor} AS SELECT * FROM {anchor} "
     f"WHERE hash({sample_key(anchor)}) % {K} < {round(f*K)}")
kept = {anchor}
for V in bfs_order(G, start=anchor):                      # nearest-to-anchor first
    incoming = [(vc, U, uc) for (vc, U, uc) in neighbours(G, V) if U in kept]
    if row_count(V) <= WHOLE_THRESHOLD or not incoming:   # small or disconnected -> keep whole
        emit(f"CREATE TABLE keep_{V} AS SELECT * FROM {V}")
    else:
        preds = " OR ".join(f"{vc} IN (SELECT {uc} FROM keep_{U})" for (vc, U, uc) in incoming)
        emit(f"CREATE TABLE keep_{V} AS SELECT * FROM {V} WHERE {preds}")
    kept.add(V)
# then re-run the non-anchor statements until no keep_* grows (step 4 fixpoint)
```

Fed TPC-H, this same loop expands to `keep_lineitem` (anchor) → `keep_orders/part/supplier` →
`keep_partsupp/customer` → `keep_nation` (OR-ed over customer+supplier) → `keep_region`; fed any other
database it expands to that schema's tables and edges. DuckDB-native, the `keep_*` sets *are* the tier
(oracle attaches them, engine shm-ingests them); in the internal parquet fallback each is `COPY`'d to
`ratio<fraction>/<table>.parquet`.

**Properties & caveats:**

- **Non-vacuous joins**: every table a kept anchor row joins to is kept, so the workload's joins
  produce rows - the point of the exercise.
- **Deterministic** (`hash(key) % K`, no `USING SAMPLE` RNG) → a tier is reproducible and cache-keyable.
- **Fan-out from the non-anchor side is a sample, not a full subtree**: a kept parent row retains only
  the child rows that were reached (the child was sampled at the anchor, not the parent). Deliberate
  cost of anchoring on the largest table; raise `f` where complete per-parent groups matter (§10).
- **Cross-edge / cycle dangling** is closed by the step-4 fixpoint pass.
- **Composite-key edges** (an edge whose two ends are multi-column) need the semi-join's `IN` over a
  column *tuple*; v1 supports single-column edges and reaches a composite-only table via its
  single-column edges instead (§11).
- **Type-preserving** in the internal parquet fallback - `COPY (SELECT * ...) TO parquet` keeps DuckDB's exact column
  types, guarded by
  [scale_parquet.py:447-484](../src/synnodb/workloads/dataset/custom_scaler/scale_parquet.py#L447-L484)
  `validate_output_dtypes`.

### 6.4 Sizing

`downscale_tiers` are **sampling ratios of the anchor**, not absolute rows and not scale factors, and
each tier is named by its ratio (`0.02`, `0.1`, …; the internal-fallback folder is `ratio0.02/` etc.) -
no `sf` labels anywhere (resolved decision 5). Realized tier size is emergent (propagation pulls in
joinable rows), so we log per-table row counts and total bytes and expose them - no silent truncation.

---

## 7. Runtime serving (mostly already done)

At serving time the engine ingests the live DuckDB tables over shm
([discovery.py:_bind_engine](../src/synnodb/duckdb_compat/discovery.py#L212-L273) +
`_aligned_arrow`). Two small gaps to make "bring a `.duckdb`" seamless:

1. **Open-the-file ingestion.** When the user does `synnodb.connect("my.duckdb", engines=...)`, the
   file's tables are already visible in the inner DuckDB, so `_tables_present` /
   `check_compatibility` already see them - discovery should bind and shm-ingest with no manual
   `CREATE TABLE`. Verify this path; the notebook currently hand-loads tables first.
2. **Provenance match.** The published engine's `source_db`
   ([manifest.py:112-115](../src/synnodb/router/manifest.py#L112-L115)) can record which DuckDB it
   was generated from, so discovery can warn on a mismatched attach. Already modeled by
   `optimize_database`.

No data-plane change - only entry wiring.

---

## 8. Impact map & phasing

End state: **DuckDB-native is the only exposed path; the parquet path survives internally as a
fallback** (§5.2, resolved decision 4). Suggested sequencing - each step independently shippable:

### Step 1 - the downscaler + entry point ✅ **implemented**

- `SynnoDB.sync_from_duckdb(...)` - the new front door (§5.1), backed by an internal
  connection-sourced `register_workload_from_duckdb`.
- `duckdb_downscale.py` - the largest-table-anchored propagation sampler (§6.3), beside
  `scale_parquet.py`, reusing its introspection helpers and adding the join-graph builder
  (query-join inference ∪ declared constraints ∪ explicit `join_relationships`).
- Validate via the **internal parquet fallback** first (sampler `COPY`s tiers to
  `ratio<fraction>/<table>.parquet`), so the whole factory is exercised end-to-end with **zero downstream
  change** - the fastest way to check the sampler and the notebook against a real DuckDB source. This
  is a dev scaffold, not a shipped mode.
- Notebook rewrite: drop `ensure_tpch_parquet` + the manual `CREATE TABLE ... read_parquet`; open (or
  build) a `tpch.duckdb` connection, call `db.sync_from_duckdb(...)`, and
  `synnodb.connect("tpch.duckdb", ...)`.

### Step 2 - DuckDB-native synthesis (the default path) ✅ **implemented**

- Add `DataSource.DUCKDB`; the oracle attaches the tier tables instead of `read_parquet`
  ([duckdb_connection_manager.py:172-175](../src/synnodb/observability/benchmark/systems/duckdb_connection_manager.py#L172-L175),
  [system_factory_olap.py:80-97](../src/synnodb/workloads/system_factory_olap.py#L80-L97)). Thread it
  through the oracle cache key (as the parquet/flat flag already is).
- Route the candidate engine through the **shm plane during synthesis** - reuse the serving-time
  `ShmHotLoadEngine` / `SYNNODB_SHM_INGEST` path
  ([process_engine.py:349-424](../src/synnodb/router/process_engine.py#L349-L424)). In-memory storage
  first. Tiers stay as ephemeral DuckDB tables; nothing is materialized to parquet.

### Step 3 - remove parquet from the remaining corners

- SSD/persistent template gets a non-parquet ingest branch (today `templates/olap/ssd` has none) so it
  too samples from DuckDB (resolved decision 4); the internal parquet fallback covers it until then.
- Published-engine packaging: the bundled standalone snapshot (`engine_publish.py:_populate`)
  references the source DB rather than shipping a parquet snapshot (resolved decision 4).

The parquet path is never deleted, only de-exposed: Step 1 uses it as a scaffold, and Steps 2-3 make
DuckDB-native the sole default while keeping the parquet code reachable internally as a fallback (and
as what the retained `register_workload_from_json` entry uses, resolved decision 6).

---

## 9. Test plan

- **Downscaler unit tests**: on a synthetic anchor + neighbours schema, assert (a) no dangling join
  keys along the traversed edges after the fixpoint pass, (b) every workload join produces > 0 rows,
  (c) determinism (same fraction → identical row sets), (d) small dims kept whole, (e) disconnected
  tables kept whole.
- **Join-graph inference tests**: feed the TPC-H `queries.json`; assert the recovered join edges
  match the known TPC-H relationships with no declared constraints and no explicit
  `join_relationships`.
- **End-to-end, native vs. fallback**: `db.sync_from_duckdb(conn)` on a `dbgen` `tpch.duckdb`
  connection; run the base-impl stage at the small tiers and confirm the correctness check is
  **non-vacuous** (joins non-empty) and passes, then benchmark at the full tier. Run it once
  **DuckDB-native** (the shipped path) and once through the **internal parquet fallback**, and assert
  the validation outcome is identical - the fallback is the cross-check that keeps the native path
  honest.
- **Golden**: the notebook runs green against a DuckDB source.

---

## 10. Resolved decisions

1. **Attach ownership → there is no file to own.** The `SynnoDB` driver is passed a **live DuckDB
   connection object**; it neither copies nor moves anything. A `sync_from_duckdb` method runs the
   ingest straight from the connection's data (§5.1). The benchmark tier is read zero-copy; the
   derived downscale tiers are ephemeral and never written back. This dissolves the
   read-only-attach vs. adopt-the-file question entirely.

2. **Join graph is built primarily from the queries** (§6.2), unioning in declared constraints and
   an explicit `join_relationships` override *when available*. Query-JOIN inference (sqlglot) is the
   lead signal - it makes the TPC-H demo work with zero user input despite `dbgen` declaring no keys.

3. **Single auto-picked anchor + full closure** to start; multi-anchor for galaxy schemas is deferred
   (§6.3).

4. **No parquet at all in the exposed path.** Sample directly from DuckDB; DuckDB-native is the only
   default mode. A parquet load/materialize path is **kept internally** as a fallback (and dev
   scaffold), never user-facing (§5.2, §8). Both remaining corners go DuckDB-native too: the published
   engine package references the source DB (no bundled parquet snapshot), and SSD/persistent storage
   gets its own non-parquet ingest branch.

5. **Move off scale factors to sampling ratios.** Tiers are named by their **sampling ratio** (`0.02`,
   `0.1`, …, plus `1.0`/`full` for the benchmark tier); the `sf1`/`sf2`/`sf<N>` labels are gone
   everywhere, **including the internal-fallback folder prefix**, which becomes `ratio<N>/` (e.g.
   `ratio0.02/`). The directory-resolution primitive and the SF-flavored spec fields are retargeted and
   renamed to match (§3, §5.2, §6.4).

6. **Keep the parquet entry path.** `register_workload_from_json` (bring-your-own-parquet) stays
   alongside the new DuckDB front door; the DuckDB path is additive. It rides the same retained
   internal parquet code from decision 4.

---

## 11. Risks

- **Propagation blow-up**: if the largest table is a *hub* everything joins to, propagation can pull
  in most of the DB. Guard: keep small dims whole below a threshold; log realized tier sizes; lower
  `f` if a tier overshoots.
- **Partial fan-out** from anchoring on the largest table (kept parents keep only sampled children,
  §6.3). Acceptable for correctness rungs; raise `f` where complete per-parent groups matter.
- **Disconnected tables** (no join path to the anchor) get no rows by propagation. Guard: keep such
  tables whole (they're not join-reachable, so usually small/independent) and log them.
- **Composite / multi-column join keys** and self-referential edges - out of scope for v1 (reject
  with a clear message), consistent with the BYO "stay simple" principle. (TPC-H's one composite
  edge, `lineitem↔partsupp`, is covered by routing `partsupp` via `part`+`supplier` in §6.3.)
- **Type fidelity** across `COPY TO parquet` (internal parquet fallback only) - covered by `validate_output_dtypes`.
- **Parquet duplication cost** on very large source DBs is now confined to the internal parquet
  fallback; the DuckDB-native default avoids it entirely.
