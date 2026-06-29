# CPP Runner

The cpp runner keeps a long-lived C++ process alive and hot-reloads shared libraries (`.so` files) between runs — avoiding process restarts while picking up freshly compiled code.

## Architecture Overview

```
Python (RunTool / HotpatchProc)
  │
  ├─ p2c pipe   ──►   ├─ libloader.so  (stage 1: load Parquet data)
  │  (RUN + query      ├─ libbuilder.so (stage 2: build in-memory DB)
  │   lines bundled)  └─ libquery.so   (stage 3: run queries)
  │
  ├─ c2p pipe   ◄───  JSON result line (batch_id + query_results)
  │
  └─ stdout/stderr (captured)
```

`HotpatchProc` launches `./db <parquet_dir>` with two inherited file descriptors (`P2C_FD`, `C2P_FD`) for control/result messages, plus ordinary stdout/stderr for logs. Query lines are no longer sent over stdin; they are bundled into the framed RUN message on p2c so they cannot drift out of sync with the run command. The C++ `db` process then forks once: the parent reads p2c and writes c2p, while the child runs the three-stage pipeline. Each stage is a separate `.so` that can be hot-reloaded independently.

## Key Files

| File | Purpose |
|---|---|
| `hotpatch_proc.py` | Process lifecycle, IPC, timeout/OOM handling |
| `proc_utils.py` | `ProcTreeTimeoutKiller` — enforces per-run timeouts |
| `pool.py` | `FastTestPool` — reuses runner processes across invocations |
| `db.cpp` | C++ main: sets up pipes, forks parent/child, orchestrates pipeline |
| `utils/plugin.hpp` | Hot-reload via ELF build-ID comparison + `dlopen` |
| `utils/pipeline.hpp` | Stage child-process management, pipe protocol |
| `utils/query_api.hpp` | `QueryResult` struct (trace + elapsed_ms per query) |
| `utils/trace.hpp` | Profiling macros: `PROFILE_SCOPE`, `TRACE_COUNT`, `TRACE_FLUSH` |

Python integration:

| File | Purpose |
|---|---|
| `../tools/run.py` | `RunTool` — compile, run, validate, return metrics |
| `../tools/compile.py` | `CompileTool` — standalone compile step |
| `compiler.py` | Incremental C++ build with dependency tracking |
| `compiler_cached.py` | `CachedCompiler` — skip recompile when sources unchanged |

## Passing Queries to the Runner

Queries are passed as lines on **stdin**, each line being one query invocation. The format is:

```
<query_id> <arg1> <arg2> ...
```

For example:
```
1 "BRAND#23" "AIR"
3 "BUILDING" "EUROPE" "AUTOMOBILE"
```

- `query_id` identifies which query to run (e.g. TPC-H query 1–22).
- The remaining tokens are the query's placeholder values (quoted strings, or `(val1,val2,...)` for IN-lists).

These args are assembled by `format_args_string()` ([query_validator_class.py:626](../tools/validate/query_validator_class.py#L626)) from pre-generated `QueryInstantiation` objects stored in the `QueryCache`. `RunTool` passes them to `HotpatchProc.run(query_lines=...)`, and the batch is sent together with the run command.

### How the C++ side reads them

Inside `libquery.so`, the `query()` function (template: [query_impl.cpp](../../prepare_repo/templates/query_impl.cpp)) receives the query lines for the current run as an explicit vector:

```cpp
std::vector<QueryResult> query(Database* db, const std::vector<std::string>& query_lines) {
  for (const auto& line : query_lines) {
    std::string query_id;
    iss >> query_id;                  // first token is query ID
    requests.push_back({query_id, line});
  }
}
```

Each per-query implementation (`run_qN()`) then parses its own arguments from the full `line` string using a generated args-parser struct (`Q<N>Args`).

## Result Files (Arrow)

Each query writes its result as an Arrow IPC file, named by request id:

```
result_<req_id>.arrow    ← result of one query invocation
```

`run_qN()` returns a `std::shared_ptr<arrow::Table>` built with [`column_egress.hpp`](cpp_helpers/column_egress.hpp): a DECIMAL output column is built as `arrow::decimal128(p, s)` **directly from the unscaled `__int128` accumulator** (never through `double` or a formatted string), so a routed result is bit-identical to DuckDB's DECIMAL; only genuinely floating columns (AVG / DOUBLE) use `double_column`. Integer output can come from narrow C++ vectors through `integer_column`, `uint64_column` handles UBIGINT, and `hugeint_column` emits HUGEINT as `decimal128(38,0)`. This is the egress counterpart to the exact `column_ingest.hpp` cast on the input side: Arrow casts decode correctly, the Database stores the narrowest correct C++ representation chosen by the storage plan, and Arrow egress restores the exact DuckDB type. These helper headers are the extension point for generic flat scalar ingest/egress support; generated implementations should improve them centrally instead of adding one-off Arrow decoding or formatting in loader/query code.

The dispatch in `query_impl.cpp` writes the table via `synnodb::write_result()` ([`result_writer.hpp`](cpp_helpers/result_writer.hpp)):
```cpp
std::shared_ptr<arrow::Table> result = run_qN(db, args);
synnodb::write_result(result, req.req_id);   // -> <SYNNODB_RESULT_DIR>/result_<req_id>.arrow
```

The destination is `SYNNODB_RESULT_DIR` when set (a `/dev/shm` directory for the shm hot-load, so the result rides shared memory back to the runtime zero-copy), otherwise `results/` relative to the runner working directory. Stale `result_<req_id>.{arrow,csv}` files are removed before each execution so a prior run's output is never validated as the current one's.

The runtime ([`process_engine.py`](../router/process_engine.py)) and the generation validator ([`run_and_check_queries.py`](../tools/validate/run_and_check_queries.py)) both read the Arrow back (`pa.ipc.open_file(pa.memory_map(...))`), which keeps DECIMAL columns as exact `Decimal` objects, and compare against the DuckDB reference (also fetched via Arrow) - so DECIMAL columns are checked for **exact** equality, only DOUBLE columns tolerantly. A legacy engine that still writes `result_<req_id>.csv` is read with the per-query output schema and the same exact-cast path, so older engines keep working.

## IPC Protocol

**p2c (Python → C++):** Binary-framed control messages. Each message starts with:
```
[uint32_t magic = "CPR1"][uint32_t action][uint64_t batch_id][uint32_t line_count][uint32_t env_count]
```

For `RUN`, the header is followed by `line_count` length-prefixed UTF-8 query lines, then `env_count` length-prefixed UTF-8 key/value pairs. For `TERMINATE`, both counts are zero. The same framed message is forwarded stage-to-stage, so the query batch and per-run environment cannot drift independently from the run command.

**c2p (C++ → Python):** A single JSON line per run, emitted by `db.cpp`.
```json
{
  "batch_id": 123,
  "exit_code": 0,
  "signal": 0,
  "query_results": [
    {"trace": "PROFILE q1_scan 1234567\n...", "elapsed_ms": 42},
    {"trace": "", "elapsed_ms": 15}
  ]
}
```

`HotpatchProc.run()` verifies the returned `batch_id`, then parses this JSON before returning to `RunTool`: its `response` string becomes `exit_code: <n> signal: <n>`, and the JSON `query_results` array becomes Python `QueryResult(trace, elapsed_ms)` objects.

Internally, the three pipeline stages communicate via binary pipes (`ipc::write_exact<T>()`). The query stage writes query-result metadata on a separate length-prefixed pipe before signalling done; the parent reads it only if the query stage did not terminate by signal:
```
[uint32_t length][JSON array bytes]
```

## Runtime Measurement

**Per-query wall-clock time** — measured in C++ with `std::chrono::steady_clock`, stored as `elapsed_ms` in `QueryResult`, returned in the JSON response on c2p.

**Ingest (build) time** — measured around `api.build()` in `db.cpp`:
```cpp
const auto t0 = std::chrono::steady_clock::now();
state.database = api.build(state.parquet_tables);
const auto t1 = std::chrono::steady_clock::now();
const float ms =
    std::chrono::duration<float, std::milli>(t1 - t0).count();
std::cerr << "Ingest ms: " << ms << "\n";
```
Python extracts this from stderr with a `"Ingest ms:"` prefix search and caches it as `runner.last_ingest_time_ms`. If the builder stage did not re-run (no `.so` change), the cached value is reused.

**Profiling traces** — available when the generated `query_impl.cpp` has been prepared for trace mode and the code is compiled with `-DTRACE`. The optimize-prep step includes `trace.hpp`, enables the generated `TRACE_RESET()`/`TRACE_FLUSH()` hooks, and changes `results.push_back({"", elapsed_ms})` to return `trace_get_and_clear()`. Inside C++ query code:
```cpp
PROFILE_SCOPE("scan_lineitem");   // RAII accumulator
TRACE_COUNT("rows_emitted", n);   // immediate counter
TRACE_FLUSH();                    // flush buffer before query_api returns
```
Uses `CLOCK_MONOTONIC` with nanosecond precision. Without that trace-mode rewrite, the `trace` field is intentionally the empty string even though `elapsed_ms` is still returned.

**Compile time** — measured by `CachedCompiler` and stored in the pickle cache file alongside the binary artefacts.

## Hot-Reload Mechanism

Each `.so` is wrapped in a `Plugin` (`utils/plugin.hpp`). Before each run cycle the C++ parent checks if the on-disk file changed:

1. **Build-ID comparison** — reads the ELF PT_NOTE section, extracts the GNU build ID, compares with the currently loaded image.
2. **Copy-on-load** — the new `.so` is copied to `.reload/lib<name>.<pid>.<counter>.so` before `dlopen` to prevent symbol-table collisions.
3. **State teardown** — if `libloader` reloads, `state.parquet_tables` is destroyed and re-loaded. If `libbuilder` reloads, `state.database` is destroyed and rebuilt.

| Stage | RunPolicy | Effect |
|---|---|---|
| libloader | OnChange | Re-runs only when .so build ID changes |
| libbuilder | OnChange | Re-runs only when .so build ID changes |
| libquery | Always | Re-runs every invocation |

## Edge Cases

### Timeout
`ProcTreeTimeoutKiller` is polled from `HotpatchProc.run()`. The timer starts only after the query-stage process exists (`db -> loader -> builder -> query`), so validation timeouts do not kill a loader or builder reload. On expiry it sends SIGKILL once to that deepest eligible descendant (rightmost-child walk). Python annotates the returned response string with a timeout message.

### Process crash / signal
When c2p reaches EOF before a response is written, the Python side calls `waitpid()` to collect exit status. `query_results` defaults to `[]`.

### Out-of-memory
Virtual memory is bounded at process start via `RLIMIT_AS` (default: 90% of system RAM). If c2p closes and the subprocess return code is negative while a memory limit is configured, `HotpatchProc.run()` reports that as a `MemoryError` response with `query_results=[]`. `RunTool` also detects `std::bad_alloc` in stdout/stderr and retries once.

### Compilation error
`CachedCompiler.build_cached()` returns an error string on failure. `RunTool` truncates it to 10 000 characters and returns it as the tool result.

### Missing result file
If `result_<req_id>.arrow` is absent after execution, `check_output_correctness()` returns an error immediately, reporting which file was missing.

### Broken pipe / child dies during stage message
`pipeline.hpp` checks pipe-write return values. If the child died before reading its RUN message, the error propagates through the done-token mechanism.

## Process Lifecycle

```
1. First call to `HotpatchProc.run()`:
   - Python creates p2c and c2p pipes
   - fork() + exec() launches ./db <parquet_dir>
   - db forks into parent (orchestrator) and child (pipeline runner)

2. Before each run:
   - Python writes one framed `RUN` message to p2c, including the batch id and query arg lines

3. Each run cycle:
   - db parent checks build IDs, reloads changed .so files
   - Stage children execute: load → build (if changed) → query
   - libquery receives the framed batch lines, runs queries, writes result_<req_id>.arrow, sends query_results via pipe
   - db parent writes JSON response to c2p
   - Python reads response, parses query_results[]

4. Shutdown:
   - Python writes one framed `TERMINATE` message to p2c
   - db parent exits cleanly, all children exit
   - Python calls waitpid(), verifies exit code 0
```

Runners are pooled in `FastTestPool` (keyed by command + scale factor + CPU affinity config), so one `db` process is reused across many LLM agent tool calls.
