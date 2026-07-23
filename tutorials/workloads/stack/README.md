# Stack cardinality-estimation benchmark → DuckDB

The **Stack** benchmark (the StackExchange dataset from the *Flow-Loss* /
Cardinality Estimation Benchmark line of work) plus its `q1`-`q16` query
workload, converted to DuckDB.

- **Data:** `so_pg13` - a PostgreSQL custom-format dump (`pg_dump -Fc`, PG 13.2,
  19,914,075,537 bytes / ~18.5 GiB), ~170 StackExchange sites unioned by
  `site_id`.
- **Schema (10 tables):** `account, site, so_user, tag, question, answer,`
  `comment, badge, post_link, tag_question`. Everything keyed by `site_id`;
  questions and answers are separate tables; tags are normalized via
  `tag_question`. snake_case columns.
- **Workload:** `so_queries/` - 16 classes, 6191 join-heavy `count(...)` queries
  filtered by `site.site_name`.

> **Not the same as SQLStorm.** This repo also carries scripts for the TUM
> [SQLStorm](https://github.com/SQL-Storm/SQLStorm) StackOverflow dataset, which
> is a *different* benchmark with a `Users`/`Posts`/`Comments` raw-dump schema.
> The `so_queries` workload does **not** run against SQLStorm. See
> [SQLStorm dataset (separate)](#sqlstorm-dataset-separate) at the bottom.

## Prerequisites

- `duckdb` CLI (tested v1.5.0) with the `postgres` extension, `docker`,
  `pg_restore`/`psql` (v16), `wget`, `python3`.
- DuckDB **cannot** read the custom-format dump directly, so the conversion goes
  through a throwaway Postgres. `pg_restore` v16 restores the v13 archive fine.

## Disk / placement

The restored Postgres database is far larger than the 18.5 GiB dump and does not
fit on local disk here (`/`=57 GB, `/tmp`=42 GB, `/home`=94 GB). Put everything -
the dump, PGDATA, and the DuckDB file - on `/mnt/labstore` (16 TB). Postgres runs
fine with PGDATA on that NFS mount.

```bash
STORAGE_DIR=/mnt/labstore/learned_db/synno_data/workloads/stack
mkdir -p "$STORAGE_DIR"
```

## 1. Download the dump

Dropbox drops the connection every ~130 MB, so resume in a loop until the file
reaches its exact expected size (a short read otherwise restores partial data
silently).

```bash
URL="https://www.dropbox.com/s/55bxfhilcu19i33/so_pg13?dl=1"   # 302-redirects; wget follows it
DEST="$STORAGE_DIR/so_pg13"
until [ "$(stat -c%s "$DEST" 2>/dev/null || echo 0)" -ge 19914075537 ]; do
  wget --continue --tries=1 --timeout=60 "$URL" -O "$DEST" || true
done
# sanity: a PG custom dump starts with the bytes "PGDMP"
head -c5 "$DEST"; echo
```

Wrapped by [`download_ce.sh`](download_ce.sh):

```bash
./download_ce.sh          # honors STORAGE_DIR
```

## 2. Convert to DuckDB

DuckDB can't read the dump, and the `pg_restore --data-only | CSV` route corrupts
multi-line `body` text through Postgres COPY escaping. So: restore into a
throwaway Dockerized Postgres, then pull each table across with DuckDB's postgres
scanner. [`convert_ce_to_duckdb.sh`](convert_ce_to_duckdb.sh) does all of it and
verifies row counts:

```bash
./convert_ce_to_duckdb.sh                       # -> $STORAGE_DIR/stack_ce.duckdb
# env knobs: STORAGE_DIR, DUMP, DB_PATH, PGPORT, JOBS, KEEP_PGDATA=1
```

What it does, and the non-obvious parts that will bite a manual redo:

1. **Start Postgres** (`postgres:16`), PGDATA bind-mounted on labstore, with
   durability turned off (`-c fsync=off -c synchronous_commit=off -c
   full_page_writes=off -c wal_level=minimal`):
   - The server is disposable and every table is read exactly once, so fsync is
     wasted work - and fsync-to-NFS latency is otherwise a real drag on the bulk
     COPY.
   - Run the container as the host uid/gid (`--user "$(id -u):$(id -g)"`),
     otherwise PGDATA ends up owned by the image's uid 70 and the host can't
     `rm -rf` it afterwards.
2. **Wait for the *TCP* server, not the init phase.** The `postgres` entrypoint
   first runs a socket-only init server and *then* restarts with TCP. A
   `docker exec ... pg_isready` passes during that init window while host
   connections still fail - so poll the host port instead:
   ```bash
   until PGPASSWORD=stack psql -h 127.0.0.1 -p 5439 -U postgres -d stack -tAc 'select 1' >/dev/null 2>&1; do sleep 1; done
   ```
3. **Restore data only - skip indexes, primary keys and foreign keys.** The dump
   carries 12 indexes + 10 PKs + 19 FKs; building/validating them dominates a
   full restore and is pure waste here, since the scanner only ever full-scans
   each table. Restore pre-data (bare tables) then data (parallel COPY), and skip
   post-data entirely:
   ```bash
   PGPASSWORD=stack pg_restore --no-owner --no-privileges --section=pre-data \
     -h 127.0.0.1 -p 5439 -U postgres -d stack "$STORAGE_DIR/so_pg13"
   PGPASSWORD=stack pg_restore --no-owner --no-privileges --section=data \
     --exit-on-error -j 8 -h 127.0.0.1 -p 5439 -U postgres -d stack "$STORAGE_DIR/so_pg13"
   ```
4. **Copy every table into DuckDB** via the scanner (no CSV round-trip):
   ```sql
   INSTALL postgres; LOAD postgres;
   ATTACH 'dbname=stack host=127.0.0.1 port=5439 user=postgres password=stack'
     AS pg (TYPE postgres, READ_ONLY);
   CREATE OR REPLACE TABLE question AS FROM pg.public.question;   -- per table
   ```
5. **Verify** `count(*)` matches between Postgres and DuckDB for every table,
   then tear down the container (and PGDATA unless `KEEP_PGDATA=1`).

Result: `stack_ce.duckdb` (~51 GB), 10 tables, ~248 M rows total:

| table | rows | | table | rows |
|---|--:|---|---|--:|
| `comment` | 103,459,958 | | `question` | 12,666,441 |
| `badge` | 51,236,903 | | `answer` | 6,347,553 |
| `tag_question` | 36,883,819 | | `post_link` | 2,264,333 |
| `so_user` | 21,097,302 | | `tag` | 186,770 |
| `account` | 13,872,153 | | `site` | 173 |

## 3. Query workload → queries (built on demand)

`so_queries/` holds the workload: 16 classes (`q1` .. `q16`), 6191 concrete
`.sql` files. Within a class every query shares one join skeleton and differs
only in its filter predicates (string/number literals, `IN (...)` lists, and in
some classes the filtered column or the comparison operator).

**Nothing here is checked in.** The raw log is large, so it is downloaded and
cached on first use, and the templates/queries are derived from it in memory. The
demo ([`tutorials/gen_full_stack_demo.py`](../../gen_full_stack_demo.py)) just
calls `build_stack_queries_json()`; the two stages below run automatically. Both
are deterministic - identical output every run for a given `so_queries/` download.

### `extract_templates.py` — `so_queries/` → template extraction

`ensure_so_queries()` downloads `so_queries.tar.zst` (~540 KB) from
<https://rmarcus.info/so_queries.tar.zst> and caches it under
`so_queries/` (git-ignored; pure-Python `zstandard` + `tarfile`, no system
`tar`/`zstd` needed). `build_templates()` then, for each class, tokenizes every
query so the join skeletons line up position for position, finds the token
positions whose value varies across the class, and turns those into `[NAME]`
placeholders - everything else becomes fixed template text. Per class it emits the
template string, the parameter names split by kind (`parameters` = filter
literals, `column_name_parameters`, `operator_parameters`), and for every concrete
query the placeholder→value dicts. Running the file as a script also writes
`stack_templates.json` (git-ignored) for inspection and round-trips every query
(all 6191 reproduce exactly).

### `gen_stack_query.py` — templates → queries

```bash
python3 -m tutorials.workloads.stack.gen_stack_query   # writes queries.json (git-ignored) for inspection
```

`build_stack_queries_json()` turns the template extraction into the bring-your-own
shape [`byo_workload`](../../byo_workload.py) / `sync_from_duckdb` consumes,
keeping the template structure fixed so **only filter literals vary**. Some
classes (`q2`, `q3`, `q8`, `q11`-`q16`) parameterized the filtered *column* and/or
the *operator*, not just literals; the generator automatically picks each such
class's dominant column+operator instantiation, bakes it into the template text,
and keeps only the queries that used it - collapsing every class to a single
filter-literal-only skeleton. The surviving queries' literal bindings become one
`tuples` parameter group per class, so at run time SynnoDB samples a whole real
`(site, tag, threshold, ...)` binding at once (with its predicates correlated
exactly as recorded, not recombined across queries):

```jsonc
{
  "q1": {
    "sql": "select count(*) from tag, site, question, tag_question where\n site.site_name=[SITE_NAME] and tag.name=[NAME] and ...",
    "param_groups": [
      { "type": "tuples",
        "placeholders": ["SITE_NAME", "NAME"],
        "values": [["'scifi'", "'steins-gate'"], ["'pm'", "'delays'"], ...] }
    ]
  }
}
```
