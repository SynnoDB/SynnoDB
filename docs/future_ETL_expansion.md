# Future extension: bespoke ETL engines

> Status: design / future work. Nothing here is built yet; it scopes a prototype and names the
> ideas worth pursuing. The goal is **fast, exact ETL** by reusing the engines we already
> generate, with a parquet-to-parquet, in-memory prototype as the first step.

## The core insight

An ETL transform **is** a SQL query, and a SQL query is exactly what we already compile into a
bespoke engine. So an ETL engine is not a new kind of artifact - it is a generated engine pointed
at a source and a sink:

```
source (parquet) -> Arrow -> bespoke engine (the transform) -> Arrow -> sink (parquet)
```

We just made that whole path Arrow-exact end to end: DuckDB/parquet Arrow in, exact `int128`
compute, `decimal128` Arrow out (`cpp_helpers/column_egress.hpp`), bit-identical to DuckDB and
held to it by the cross-check. ETL reuses that path; the only new parts are the *framing* (an
entry point, the source/sink wiring) and, later, *scale* (streaming, spill).

## Value proposition

- **Fast**: a bespoke compiled engine for a specific transform beats a general engine's
  interpreter/operator overhead (this is what the factory already measures as bespoke-vs-DuckDB
  speedup). Generate the transform once; run it on production data many times.
- **Exact**: every engine is validated bit-for-bit against DuckDB at generation, and a *sampled*
  production cross-check (`RouterPolicy.cross_check_rate`) can catch data-dependent edge cases and
  quarantine the engine, falling back to DuckDB. Hand-tuned ETL gives you speed *or* trust; this
  gives both.

## What already exists (the foundation, reused as-is)

- **Engine generation** (the factory): a SQL query plus a dataset schema -> a fast bespoke C++
  engine. The workload-agnostic / bring-your-own path already accepts arbitrary SQL + schema, so a
  transform engine is "just" a BYO workload.
- **Arrow-exact data path**: parquet ingest (`ReadParquetTable`) or a DuckDB-Arrow hot-load over
  shm; exact `int128` compute; `decimal128` Arrow egress written as `result_<req>.arrow`.
- **`write_parquet` sink**: `con.execute(transform).write_parquet(path)` writes the exact Arrow
  result to parquet with types preserved (`duckdb_compat/connection.py`).
- **Drop-in router + close-the-loop discovery**: `import synnodb as duckdb`; a query routes to its
  engine the moment it is published, otherwise DuckDB. New engines are auto-discovered with no code
  change.
- **`optimize_database` / `synno-<db>`**: bind + publish an engine over a dataset; `mount` exposes a
  bundled snapshot as views.

So **parquet -> parquet is roughly 80% there**: the engine reads parquet, computes, and
`write_parquet` writes the result. What's missing is the ETL framing and row-level-transform
generation - not the data path.

## Prototype scope (now): parquet -> parquet, in-memory

In scope:
- A single transform (one SQL `SELECT`: filter / project / join / aggregate) read from parquet,
  written to parquet, with the result bit-identical to DuckDB.
- Row-level transforms (output a row per qualifying input row), not just aggregations.
  `column_egress` already handles this (build per-column vectors, `make_table`).
- A thin entry point so the recipe is one call.
- Reuse the existing generation + validation + `write_parquet`.

Explicitly **not** in scope yet (named in "Out of scope" below): streaming / spill-to-disk, scale
beyond RAM, S3 sources, Iceberg, partitioned multi-table output.

## Architecture

The prototype is a thin layer over what exists. The end-user recipe is essentially:

```python
import synnodb as duckdb
con = duckdb.connect(engines="<dir holding the transform engine>")
# mount the source(s) - local parquet now, S3 later
con.duckdb.execute("CREATE VIEW src AS SELECT * FROM read_parquet('in.parquet')")
# the transform routes to the bespoke engine (exact) or falls back to DuckDB
con.execute(transform_sql).write_parquet("out.parquet")
```

A focused `synnodb.etl(transform_sql, inputs={name: parquet}, output=path, *, engines=...)` wraps
this: mount inputs, route the transform, write the sink - and (optionally) trigger generation of
the engine if one is not yet published, falling back to DuckDB until it is (the close-the-loop
mechanism).

Components and where they live:
- **Source**: `read_parquet` views today (`db_io.export_tables_to_parquet` is the inverse).
  An S3 reader is future work (Arrow's S3 filesystem or pre-download).
- **Transform engine**: generated via the BYO workload path; published like any engine.
- **Egress**: `column_egress` -> `.arrow` -> `write_parquet`. For large outputs, a direct C++
  parquet writer (`parquet::arrow::WriteTable`; parquet is already linked) avoids the IPC + Python
  hop - the first scale step, and the seed of streaming egress.
- **Validation**: at generation (exact vs DuckDB). Optional sampled cross-check in production.

## Milestones

- **M1 - parquet -> parquet, one transform.** A `synnodb.etl(...)` entry; generate a row-level
  transform engine for a real SQL transform; verify the parquet output equals DuckDB exactly and is
  faster. In-memory only.
- **M2 - a small DAG.** Chain transforms (model A -> model B): each model's SQL -> its engine ->
  intermediate (Arrow in memory, or parquet) -> the next model. Resolve order from dependencies.
- **M3 - dbt adapter.** Drive the DAG and the SQL from dbt (see below).
- **M4 - Iceberg sink.** parquet -> Iceberg commit (pyiceberg), once parquet works. This is
  deliberately last: the engine and parquet are the hard, valuable parts; the Iceberg commit is a
  catalog/metadata layer on top.

## The dbt dream scenario

dbt already does what we want at the *definition* layer: models are SQL `SELECT`s plus a
materialization, wired into a dependency DAG. We supply the *execution* layer (bespoke engines).

The clean fit, and a critical caveat:
- Because SynnoDB is a DuckDB drop-in, a `dbt-duckdb` profile pointed at `synnodb` would route each
  model's compiled SQL through the router - bespoke when an engine exists, DuckDB otherwise, exact
  either way. That is the killer demo: **transparent acceleration of an existing dbt project**.
- **Caveat (do not hand-wave this):** dbt materializes via `CREATE TABLE/VIEW AS SELECT`, which the
  write-block rejects. So a real `dbt-synnodb` adapter must split each model into *route the
  `SELECT`* (the transform, to the engine) and *materialize the result* (the sink) - the
  materialization goes through the `.duckdb` escape hatch or `write_parquet`, not the blocked
  surface. The transform is accelerated; the write is an explicit sink. M3 is this adapter.
- **Close-the-loop adoption.** A model runs on DuckDB on day one; in the background we generate its
  engine; the next run auto-routes to it with no project change (this is exactly the auto-discovery
  we already ship). Zero-friction: correctness from day one, speed when the engine is ready.

In-memory only for the prototype: intermediate model outputs stay in Arrow / memory (or a parquet
hand-off) between models. Spilling and out-of-core DAGs are future work.

## Other ideas worth pursuing (ranked by leverage)

1. **Selective / cost-based generation.** Generation costs an LLM run, so only generate engines for
   the expensive, hot, repeatedly-run models; leave cheap models on DuckDB. The router already
   falls back transparently, so this is a policy, not a rewrite.
2. **Model fusion.** Fuse a chain of models (A -> B -> C) into one engine so intermediates are never
   materialized - the classic ETL win, and a natural target for the generator (it already sees the
   whole SQL). Bigger speedups than per-model engines.
3. **Predicate / projection pushdown to the source.** Read only the needed columns and row-groups
   from parquet (Arrow's parquet reader supports it), so the engine never ingests data the
   transform discards. Largest I/O win for selective transforms.
4. **Incremental models.** Generate engines that take a partition / watermark filter and process
   only new data (dbt incremental). Pairs with cost-based generation.
5. **Sampled production cross-check as a safety net.** Keep `cross_check_rate > 0` on a fraction of
   output; a data-dependent divergence quarantines the engine and serves DuckDB. Exactness as an
   operational guarantee, not just a generation-time check.
6. **Engine registry reuse across runs.** Publish engines (`synno-<model>`) and reuse them across
   nightly runs - generate once, run forever - so the LLM cost amortizes to near zero per run.
7. **Direct C++ parquet (then streaming) egress.** The `result_writer` parquet variant -> writing
   parquet row-groups as the engine produces them -> the path to out-of-core output.
8. **Exactness-diff tooling.** When an engine *does* diverge from DuckDB, show the offending
   rows/columns (we already have exact row comparison in the validator) - turns a red build into a
   precise fix.
9. **Iceberg time-travel / schema evolution.** Once the Iceberg sink exists, snapshots and schema
   evolution come largely for free from the table format.

## Risks and open questions

- **Scale.** The engine is in-memory: the loader materializes input and `column_egress` materializes
  the whole output. Large transforms need streaming ingest + egress and spill. The prototype caps at
  RAM on purpose; this is the main thing that separates "prototype" from "product."
- **Transform coverage.** Engines implement a SQL `SELECT`. Most ETL is query-shaped, but procedural
  / multi-step / UDF-heavy logic is out of scope until expressed as SQL.
- **Generation cost vs reuse.** Bespoke engines pay off only when reused; one-off transforms are not
  worth generating. Cost-based generation (idea 1) is the mitigation.
- **dbt materialization semantics.** The adapter must faithfully reproduce dbt's table/view/incremental
  materializations through the sink, not the blocked surface (see the caveat above).
- **Null / overflow edge cases on production data.** Validation is on a sample; production data may
  hit `int128` overflow or null patterns the sample missed. The sampled cross-check + quarantine is
  the guard.

## Explicitly out of scope (for now)

Streaming / batched execution, spill-to-disk and out-of-core DAGs, S3 (and other remote) sources,
the Iceberg commit layer, partitioned / multi-table output, and procedural (non-SQL) transforms.
The prototype is parquet -> bespoke engine -> parquet, in-memory, exact.
