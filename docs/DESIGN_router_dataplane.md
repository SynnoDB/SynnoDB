# SynnoDB - Router / Engine / DuckDB data-plane & communication design

> **Living document.** Updated as the implementation lands. The
> **Implementation status** table at the bottom is the source of truth for what is
> real vs planned. Companion to the build plan in
> [.plans/duckdb_drop_in_router.md](../.plans/duckdb_drop_in_router.md).

> **Realized architecture (read this first).** The out-of-process engine is the
> factory-generated ``db`` binary held warm by the framework's own ``HotpatchProc``
> runner - it already had a persistent warm loop and a framed control protocol over
> dedicated pipe fds, so serving reuses it rather than inventing a second control plane.
> The Python front is ``ShmHotLoadEngine`` / ``ProcessEngine`` in
> [router/process_engine.py](../src/synnodb/router/process_engine.py); the router
> auto-discovers published engines in
> [duckdb_compat/discovery.py](../src/synnodb/duckdb_compat/discovery.py). Concretely:
> the parent ingests the live DuckDB tables once as Arrow into a ``/dev/shm`` directory
> named by ``SYNNODB_SHM_INGEST``; the generated loader maps each ``<table>.arrow``
> zero-copy; the engine writes its answer as ``result_<req_id>.arrow`` in the same
> directory. The earlier standalone ``WorkerEngine`` + JSON ``worker_protocol`` design
> (sections 5 and 7 below describe its *logical* contract) was **removed**: it duplicated
> the warm loop the binary already has and its reference worker ran DuckDB, not the
> bespoke engine. The logical message/sequence below still describes the contract; the
> *transport* is ``HotpatchProc``'s framed protocol + the shm directory, not a bespoke pipe.

## 1. Purpose

SynnoDB is a drop-in replacement for the DuckDB Python client: a user does
`import synnodb as duckdb` and their code keeps working. Under the hood, each
**eager, read-only SQL string** is shown to a **router**. If the router recognizes
the query as one of its registered **templates** and all safety guards pass, it is
served by a bespoke **C++ engine**; otherwise - always - it is served by an
embedded **DuckDB**, which is also the canonical store ("source of truth").

This document specifies **who the actors are, how they share data, and how they
communicate**. It is deliberately precise about process boundaries, memory
ownership, and the wire/IPC contracts, because those are where correctness and
performance live.

## 2. Actors and process boundaries

```
            ┌──────────────────────────── user Python process ───────────────────────────┐
            │                                                                              │
   import   │   synnodb (duckdb_compat)                                                    │
 synnodb as │   ┌─────────────────────┐     ┌──────────────────────────────┐              │
  duckdb    │   │ SynnoConnection      │     │ QueryRouter                  │              │
            │   │  (proxies a real     │────▶│  normalize → match → guards  │              │
            │   │   DuckDBPyConnection)│     │  → decide: bespoke | duckdb  │              │
            │   └──────────┬───────────┘     └───────┬───────────────┬──────┘              │
            │              │                         │               │                     │
            │              │ fallback / source-of-truth              │ bespoke             │
            │              ▼                         ▼               ▼                     │
            │   ┌─────────────────────┐   ┌────────────────┐   ┌─────────────────────┐    │
            │   │ DuckDB (in-process) │   │ DuckDBBackend  │   │ EngineWorkerPool    │    │
            │   │  the canonical data │   │ (.arrow export)│   │  (control plane)    │    │
            │   └─────────────────────┘   └───────┬────────┘   └──────────┬──────────┘    │
            │                                     │ Arrow                 │ control msgs   │
            └─────────────────────────────────────┼───────────────────────┼───────────────┘
                                                  │                       │ (pipe: tiny)
                              shared memory (/dev/shm, Arrow IPC) ─────────┼──────────────┐
                                                  │                       │              │
            ┌───────────────────── C++ engine worker process ─────────────▼──────────────▼┐
            │   hotpatch host  ──dlopen──▶  generated plugin (load / build / run_qN)       │
            │   reads ingest segment (zero-copy views) → Database* → writes result segment │
            └──────────────────────────────────────────────────────────────────────────────┘
```

Three actors:

1. **Router + DuckDB - in the user's Python process.** The router (`synnodb.router`)
   and the embedded DuckDB connection live in-process. DuckDB is both the **fallback
   executor** and the **source of truth** the engine ingests from. There is no
   network hop for the fallback path.
2. **C++ engine worker - a separate child process.** One persistent **warm**
   subprocess per engine (reusing the existing `HotpatchProc`/`HotpatchPool`). It
   `dlopen`s the generated plugin, holds the ingested tables resident, and answers
   queries. **Separate process = crash isolation:** a segfault kills the child, not
   the user's interpreter; the router catches it and falls back to DuckDB.
3. **The generated plugin - code inside the worker.** Per-engine C++ produced by the
   factory: `load()` (ingest), `build()` (storage), and `run_q<id>()` (execution).
   The router never calls it directly; it talks to the worker.

## 3. Two planes: control vs data

A hard rule, for both performance and safety:

| Plane | Carrier | Carries | Size |
|------|---------|---------|------|
| **Control** | pipe (the worker's existing control channel) | commands & acknowledgements: `LOAD epoch`, `RUN query_id + placeholders`, `RESULT ready: segment,len`, `ERROR msg` | tiny (bytes–KB) |
| **Data** | shared memory (`/dev/shm`, Arrow IPC layout) | the actual table data (ingest) and result batches (egress) | large (MB–GB) |

**Bulk data never crosses a pipe.** It is placed in shared-memory segments that
both processes `mmap`; only small control messages traverse the pipe. (Until the
shm plane lands - see status - a transitional file/`/dev/shm`-file path is used,
but never a pipe for bulk data.)

## 4. Data sharing: where bytes live and who owns them

### 4.1 Ingestion (DuckDB → engine), once per epoch at `connect()`

- **Source of truth:** the user loads data into DuckDB normally. The engine never
  reads parquet; it ingests **from DuckDB**.
- **Export:** `DuckDBBackend` runs `SELECT * FROM <table>` and gets Arrow via
  `fetch_record_batch()`/`fetch_arrow_table()`.
- **Placement:** the Arrow IPC bytes are written into a shm segment
  `/dev/shm/synnodb-<owner_pid>-<engine_id>-<epoch>-ingest-<table>`. This is the
  **one residual copy** (DuckDB's heap → shm); DuckDB allocates its own buffers, so
  it cannot be eliminated through the public API.
- **Read (engine):** the generated `load()` maps the segment
  (`arrow::io::MemoryMappedFile` + `arrow::ipc::RecordBatchFileReader`) so the
  `arrow::Table`s are **zero-copy views** into shm. `ParquetTables` points into shm.
- **Ownership:** the **Python parent** creates and `shm_unlink`s the segment (the
  engine may be `SIGKILL`'d and cannot self-clean). Segment names are injected into
  the child via `extra_env` (like the existing `STORAGE_DIR`).

### 4.2 Egress (engine → Python), per query

- **Typed, exact egress:** `run_q<id>()` accumulates each output column into a typed
  C++ vector and builds the result `arrow::Table` with `cpp_helpers/column_egress.hpp`
  (`make_table` over `decimal/int/double/string/bool/date/timestamp` columns), which
  emits the exact DuckDB/Arrow type - including NULLs and decimal256 - via
  `arrow::compute::Cast`, the symmetric counterpart of `column_ingest.hpp`. (This
  replaces the legacy `vector<vector<string>>` + CSV.)
- **Placement:** the SoA columns are built in a shm **result arena** and framed as
  Arrow IPC; the worker sends `RESULT ready: segment,len` over the control pipe.
- **Read (Python):** the router maps the result segment and wraps it as a
  `pyarrow.Table` **zero-copy on the read side**.
- **Bounds:** the result arena is size-bounded; an oversized result either grows
  (new segment) or falls back to DuckDB for that query.

### 4.3 The fallback / source-of-truth path

When the router does not route (policy off, parse miss, template miss, a failing
guard, an engine error, or a write), it calls DuckDB in-process and returns DuckDB's
own result object/array. No shm, no worker - the normal DuckDB path, unchanged.

## 5. Communication protocol (control plane)

The worker is driven over its control channel with framed messages. Logical
messages (transport-agnostic):

- **`LOAD { epoch, tables: [{name, segment}] }`** → worker maps ingest segments,
  runs `load()`/`build()`. Ack: `LOADED { epoch }` or `ERROR`.
- **`RUN { query_id, placeholders: {name: value}, result_segment }`** → worker runs
  `run_q<id>()`, writes the result, replies `RESULT { segment, len, rows, elapsed_ms }`
  or `ERROR { message }`.
- **`PING`/`PONG`** for health; **`TERMINATE`** for graceful shutdown.

Placeholders are typed per the engine manifest (mirrors the existing `Q<id>Args`
input struct). Values come either from DuckDB-style bound parameters (`?`, `$name`)
mapped directly, or from literals extracted from the SQL via sqlglot.

## 6. The result/type contract (end-to-end fidelity)

The drop-in promise is that a routed result is **indistinguishable** from DuckDB's:

1. The canonical schema for a template is captured **once** from DuckDB's
   `description` (column names + types) at registration.
2. The engine's `Q<id>Out` columns are generated **to that schema**, so the engine
   is type-locked to DuckDB.
3. `adapt.py` builds a `SynnoResult` from the engine's Arrow that exposes the same
   `description`, `fetchone/all/many`, `df`, `arrow`, `pl`, `fetchnumpy` as DuckDB.
4. In `sampled` mode, ~`cross_check_rate` of routed queries are **also** run on
   DuckDB and compared (set- or order-semantics); a mismatch quarantines the
   template and returns DuckDB.

Caveat: row **order** is only guaranteed when the query has `ORDER BY`; otherwise
results are set-equal (as SQL permits), matching DuckDB's own non-determinism.

## 7. One query, end to end (sequence)

```
user: con.execute("SELECT ... WHERE x = ?", [42]).fetchall()
  │
  ├─ SynnoConnection.execute(sql, params)
  │     └─ QueryRouter.route(sql, params, conn) ── RouteTrace ──┐
  │           1 policy gate            (off? → DuckDB)          │
  │           1a read-only block       (write? → DuckDB+warn)   │
  │           2 normalize (sqlglot)    (parse fail? → DuckDB)   │ verbose
  │           3 registry.match         (miss? → DuckDB)         │ logging
  │           4 guards                 (fail? → DuckDB)         │
  │           5 worker.run(placeholders) ── control: RUN ───────┤
  │                 worker reads ingest shm (zero-copy),        │
  │                 runs run_q<id>(), writes result shm,        │
  │                 replies RESULT seg,len                      │
  │           6 adapt result shm → SynnoResult                  │
  │           7 [sampled ~10%] also run DuckDB; compare + speedup
  │     └─ stores current result on the connection (cursor model)
  └─ .fetchall() → from SynnoResult (bespoke) or DuckDB (fallback)
```

## 8. DuckDB-compat surface (process-local)

- `SynnoConnection` **proxies** a real `DuckDBPyConnection` (composition +
  `__getattr__`). It is *not* a subclass - pybind11 forbids adopting an existing C
  connection - so `isinstance(con, duckdb.DuckDBPyConnection)` is **False**; the
  `con.duckdb` property returns the real connection for libraries that require it.
- DuckDB's connection **is** the cursor: `execute()` returns the connection and
  `fetch*()` reads the last result. `SynnoConnection` mirrors this: `execute()`
  returns `self` and holds the current result (bespoke `SynnoResult` or delegated to
  DuckDB).
- **Only eager SQL text is intercepted** (`execute`, module `sql`/`execute`).
  Everything else - the relational API (`con.sql(...)` lazy relations),
  `register`/`read_csv`/`read_parquet`, DataFrame/Arrow/Polars egress, `PRAGMA`/`SET`,
  exceptions, `typing` - is delegated **verbatim** to DuckDB. That is where DuckDB's
  long-tail compatibility comes from, for free.
- **Namespace parity:** `synnodb` re-exports DuckDB's entire public namespace
  (exceptions, `typing`, `__version__`, …) and overrides only `connect/sql/execute`.
  `except duckdb.CatalogException` etc. keep working.

## 9. Safety invariants

- **Fallback-always:** the router never raises for a routing/engine reason; any
  failure path returns the DuckDB result. Only a genuine DuckDB error propagates.
- **Zero-config == DuckDB:** with no engines registered, behavior is byte-identical
  to DuckDB. Switching can only add speed, never change results.
- **Crash isolation:** an engine segfault → broken pipe → fall back + respawn; the
  user's process survives.
- **Read-only (v1):** mutations are not accelerated; they are detected, run on
  DuckDB, and any engine bound to a touched table is invalidated.
- **Light runtime:** the drop-in imports only `duckdb`, `pyarrow`, `sqlglot`; the LLM
  factory is an optional `synnodb[factory]` extra and is never imported by the router.

## 9a. Observability - chasing errors

Turn on full tracing with one call:

```python
import synnodb
synnodb.enable_debug_logging()   # DEBUG on the whole 'synnodb' logger tree
```

Logger tree (all children of ``synnodb``; tune individually):

| Logger | Emits |
|--------|-------|
| `synnodb.router` | every routing decision + full `router-detail` (decision, reason, **each guard's verdict**, timings, cross-check result, speedup, sql) |
| `synnodb.router.registration` | each binding: tables, **schema fingerprint**, captured **output schema**, **normalized SQL** (the match key) |
| `synnodb.router.process_engine` | engine spawn (warm-runner cmd/cwd), shm ingest (per-table rows/bytes, total MiB, ingest dir), run (query_id, rows/cols), engine errors |
| `synnodb.router.shm` | shm segment writes (rows/bytes), orphan sweeps |

Engine faults log a full traceback at DEBUG (`synnodb.router`), and a cross-check
mismatch is always a WARNING with the offending SQL. A "why didn't my query route?"
question is answered by the per-guard verdicts in `router-detail`, and a "why didn't
it match a template?" by comparing the logged `normalized` keys.

## 10. Implementation status

| Component | Module | Status |
|-----------|--------|--------|
| DuckDB-compat surface (proxy, result, namespace/exception parity) | `synnodb.duckdb_compat` | **done (Phase 1)** ✓ |
| Router policy + trace + verbose logging | `synnodb.router.policy`, `.observe` | **done (Phase 1)** ✓ |
| QueryRouter full pipeline (gate→read-only→normalize→match→guards→execute→cross-check) | `synnodb.router.router` | **done (Phase 1/2)** ✓ |
| Template registry (match/dirty/quarantine) | `synnodb.router.registry` | **done (Phase 1)** ✓ |
| Query normalization + classification (sqlglot) | `synnodb.router.normalize` | **done (Phase 1)** ✓ |
| Guards (engine-ready, SELECT-only, dirty, schema, arity) | `synnodb.router.guards` | **done (Phase 1)** ✓ |
| Dependency split (light runtime vs `[factory]`) | `pyproject.toml` | **done (Phase 1)** ✓ |
| Bespoke engine interface + Python test-double | `synnodb.router.engine` | **done (Phase 2)** ✓ |
| Pluggable backend (DuckDB now, Postgres later) | `synnodb.router.backend` | **done (Phase 2)** ✓ |
| Bespoke execution + sampled cross-check + circuit breaker | `synnodb.router.router` | **done (Phase 2)** ✓ |
| Result adaptation (Arrow→SynnoResult) + result equality | `synnodb.router.adapt` | **done (Phase 2)** ✓ |
| Live registration (schema/fingerprint/output capture) | `synnodb.router.registration` | **done (Phase 2)** ✓ |
| Engine manifest + (de)serialize + register + compatibility gate | `synnodb.router.manifest` | **done (Phase 2)** ✓ |
| Shared-memory zero-copy Arrow transport (write/read, lifecycle, orphan sweep) - reference producer/consumer pinning the C++ shm wire format | `synnodb.router.shm_transport` | **done** ✓ |
| Out-of-process serving engine: warm ``db`` binary over ``HotpatchProc``, ``/dev/shm`` Arrow ingest (``SYNNODB_SHM_INGEST``) + ``result_<req_id>.arrow`` egress, crash-isolated, auto-discovered | `synnodb.router.process_engine` (`ProcessEngine`, `ShmHotLoadEngine`), `duckdb_compat.discovery` | **done** ✓ |
| Content-addressed engine id + manifest builder + **factory-side writer** | `synnodb.router.manifest` | **done (Phase 0/2)** ✓ |
| C++ `ReadArrowTableFromShm` / `WriteArrowTableToShm` (zero-copy ingest+egress) | `cpp_helpers/shm_arrow_{loader,writer}.hpp` | **done - compiled & round-tripped vs Python (libarrow 23.0.1)** ✓ |
| Typed, exact Arrow egress (decimal128/256, int widths, float, bool, date, timestamp, NULLs) via `Cast` | `cpp_helpers/column_egress.hpp` | **done - compiled & egresses exact typed Arrow vs DuckDB** ✓ |
| Wire shm headers + egress into the live engine build (compiler + templates): the in-memory loader emits an `if (shm_ingest_enabled())` branch, result via `WriteArrowTableToShm` | `prepare_workspace_olap._gen_table_reads`, `parquet_reader.cpp`, `result_writer.hpp` | **done** ✓ (validated E2E: real q1q6byo engine serves Q1 over `/dev/shm`, bit-exact vs DuckDB) |
| Factory auto-publishes the finished engine for auto-discovery, in-memory engines as shm-capable with `expected_tables` | `main._publish_generated_engine`, `workloads.engine_publish`, `optimize.optimize_database` | **done** ✓ |

**Test coverage.** Python: `test_duckdb_compat.py` (drop-in conformance), `test_router.py`
(policy/normalize/registry/guards/result), `test_router_e2e.py`
(route/cross-check/quarantine/breaker/fallback matrix), `test_manifest.py`
(round-trip/register/compat/content-addressing/**factory→runtime loop**),
`test_shm_transport.py` (zero-copy + lifecycle + orphan sweep), `test_router_discovery.py`
(auto-discovery + binding), `test_factory_publish_shm.py` (auto-publish stamps the shm plane
+ `expected_tables`). **Real compiled engine:** `test_shm_hot_load.py` drives the local
q1q6byo `db` binary through `synnodb.connect()` over both planes (`/dev/shm` hot-load and the
parquet snapshot), bit-exact vs DuckDB. **Real C++ helpers:** `test_cpp_shm.py` (compiles
`shm_arrow_{loader,writer}.hpp`, round-trips both directions vs the Python transport, 1 M-row
table), `test_column_egress.py` (compiles `column_egress.hpp`, verifies exact typed Arrow
egress - decimal128/256, narrowed ints, float32, bool, date, timestamp, real NULLs).

_Status: Phases 1-3 + Phase-0 implemented and validated end to end, including the C++ data
plane wired into the live engine build and the factory auto-publish. The standalone
``WorkerEngine`` / ``worker_protocol`` design was removed in favour of reusing the generated
binary's existing warm-runner protocol (``HotpatchProc``); see the realized-architecture note
at the top._
