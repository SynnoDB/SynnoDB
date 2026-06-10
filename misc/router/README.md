# PostgreSQL Router PoC

This folder contains a minimal plaintext PostgreSQL router with standalone, embedded, and analysis tooling.
It only works on unencrypted PostgreSQL traffic; SSL/TLS interception is not implemented.


## Components

- Docker server: `docker-compose.yml` starts Postgres on host port `15433`
- Router: `pg_router.py` starts the router on `127.0.0.1:5432` by default
- Client: `demo_client.py` sends a small set of sample queries through the router
- Embedded demo: `demo_embedded.py` starts the router in-process on `127.0.0.1:5432`
- Analysis: `analyze_captures.py` finds repeated query shapes in `queries.jsonl`
- Tests: `run_tests.py` runs the full test suite
- Output: query metadata is appended to `queries.jsonl` and rows are stored in `results/`


## Standalone

In separate terminals:

```bash
docker compose up
python3 pg_router.py
```

Equivalent package-style entrypoint:

```bash
python3 -m pgrouter
```

If this subdirectory is part of a `uv`-managed project, install the router dependency set first:

```bash
uv sync
```

In another terminal:

```bash
python3 demo_client.py
```

This writes query metadata to `queries.jsonl` and result rows to `results/`.


## Embedded

Run `python3 demo_embedded.py` and then point `demo_client.py` at the same listen port, `5432` by default.
See [demo_embedded.py](demo_embedded.py) for a minimal in-process example with normal query hooks, streaming hook rows, and a stateful transaction hook.


## Capture

In standalone router mode, explicit transaction blocks are captured as one `queries.jsonl` record with `query_source: "transaction"` and a `statements` list. Statement rows are written to per-statement result files referenced from those `statements` entries. Single autocommit statements are still captured individually.

By default, each proxy start truncates `queries.jsonl` and clears `results/`. Use `--append` to keep existing captures:

```bash
python3 pg_router.py --append
```

Result files default to JSON. To write them as pandas pickle files instead:

```bash
python3 pg_router.py --result-file-format pickle
```


## Tooling

Run the full test suite with:

```bash
python3 run_tests.py
```

The JDBC integration test runs automatically when a PostgreSQL JDBC jar is available via `PGJDBC_JAR` or at `tests/jdbc/lib/postgresql.jar`.

Inspect existing captures with:

```bash
python3 inspect_captures.py --show-rows 3
```

Analyze captures for repeated query patterns with:

```bash
python3 analyze_captures.py [--aggregate]
```

The analyzer is transaction-aware: transaction records are analyzed as a whole block, and each SQL statement in the transaction is normalized before grouping.


## Todo

- SQL Transaction Handling
