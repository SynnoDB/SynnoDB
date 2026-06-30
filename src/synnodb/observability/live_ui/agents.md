# live_ui — Live Run Dashboard

An in-process HTTP server + single-page dashboard that streams run statistics in real time during an optimisation run.

## How it works

`LiveDashboardDrain` (`live_dashboard.py`) is a `DataDrain` subclass. When instantiated it spins up a background daemon thread running a plain `http.server` on `0.0.0.0:8765` (auto-increments if the port is taken). The main process calls `emit(metrics, step)` after each turn; the server holds that data in memory and exposes it via `/api/stats`.

`StandaloneDashboard` can serve the same UI while reading data from a local DuckDB file, W&B run history, or a remote live dashboard API on another host.

The browser polls `/api/stats` every 3 seconds and updates the UI without a page reload.

## Files

| File | Purpose |
|---|---|
| `live_dashboard.py` | Python backend — `LiveDashboardDrain`, in-memory store, HTTP server, `/api/stats` endpoint |
| `index.html` | Page shell — layout, stat cards, chart canvases, sidebar/log panel wiring |
| `style.css` | Dark-theme styles |

The frontend is split across `js/`. Files are loaded as plain `<script>` tags (no module system) in dependency order; shared mutable state lives in `state.js`.

| File | Purpose |
|---|---|
| `js/state.js` | Shared mutable globals: last poll snapshot, chart instances, mode flags, time-travel cursor, hover state |
| `js/util.js` | Pure helpers — `esc`, `fmtCost`/`fmtTime`/`fmtNum`/`fmtPieTime`/`fmtTimelineTick`, `parseJsonField`, `normalizeQueryId`/`parseQueryIds`, `isMetricTrue`/`isMetricFalse`, `addAxisHeadroom`, `getSegmentBounds` |
| `js/sections.js` | Section colour palette + `getSections` + `setHoveredSection` + `sectionBgPlugin` (Chart.js) |
| `js/metrics.js` | Per-query runtime extraction and cumulative speedup computation (`computeSpeedupSeries`, `getQueryRuntimes`, `getQueryAxisMax`) |
| `js/chart-timeline.js` | Main timeline chart (tokens / LOC / speedup), time-travel cursor + drag scrubbing, `correctnessAlignPlugin` |
| `js/chart-query.js` | Per-query speedup/runtime bar chart with inline legend and `Speedup = 1` reference line |
| `js/chart-dist.js` | Modal charts: pie of wall-clock per type, stacked-area cumulative time, call-count bar |
| `js/log.js` | Activity log panel — type metadata, per-type `logDesc`/`logBody`, incremental `updateLog` |
| `js/cards.js` | Header meta, KPI cards, turn timer, prompts list, correctness strip, cost-mode toggle |
| `js/source.js` | Standalone source selector (W&B / DuckDB / remote API), URL-param sync, cluster auto-discovery |
| `js/controls.js` | Wiring for prompt-list hover, distribution modal, panel collapse, chart-mode toggles, `Esc` shortcut |
| `js/code-inspector.js` | "Generated code" modal — fetches `/api/files`, renders a collapsible workspace tree, loads file contents from `/api/file` |
| `js/main.js` | Boot — initial reload-time stamp, `poll()` loop, default modes, `setInterval` for poll + timer |

## `/api/stats` response shape

```json
{
  "steps": [0, 1, 2, ...],
  "data": {
    "0": { "type": "llm", "input_tokens": 1234, ... },
    "1": { ... }
  }
}
```

`steps` is a sorted list of integer turn indices. `data` keys are step numbers as strings.

## Code inspector endpoints

The "Generated code" header button browses the run's generated-code workspace, served by the same dashboard server:

| Endpoint | Returns |
|---|---|
| `GET /api/files` | `{"available": bool, "root": str, "files": [<workspace-relative path>, ...]}` — a flat, sorted file list (`.git`/caches/`node_modules` pruned before descent; symlinks ignored). `available` is `false` when the source has no live workspace. |
| `GET /api/file?path=<rel>` | `{"path", "content", "size", "truncated"}` for text files, or `{"path", "binary": true, "size"}` for binaries. Reads are bounded to `_WORKSPACE_MAX_BYTES`; path-traversal or symlink escape outside the workspace returns `404`. |

The workspace is whatever the backend genuinely operates in — it is never passed to the dashboard out of band:

- **`LiveDashboardDrain`** (in-process live run) is given its `workspace_dir` by `main.py`, which resolved it from `--workspace_dir` / `SYNNO_WORKSPACE` (`./output`). It serves files from there directly.
- **`StandaloneDashboard`** only enables the inspector when its source is a **remote live dashboard** (`api_url`), in which case it proxies `/api/files` and `/api/file` to that remote drain. For DuckDB and W&B sources there is no live workspace, so the endpoints report `available: false`.

The frontend mirrors this: the "Generated code" header button is shown only for the in-process live run and remote sources, and hidden for DuckDB / W&B (see `updateHeaderMeta` in `js/cards.js`).

## Key metric keys consumed by the UI

| Key | Used for |
|---|---|
| `type` | Log entry badge (`llm`, `apply_patch`, `shell`, `compile`, `validate`, `compaction`) |
| `input_tokens` | Timeline chart — Input Tokens series |
| `code/loc` | Timeline chart — Code Size series |
| `total/cost_usd` | Cost card and per-stage cost delta in sidebar |
| `total/runtime` | Runtime card and per-stage time delta in sidebar |
| `current_prompt_descriptor` | Section coloring + sidebar stage list |
| `validation/correct` | Correctness strip (green/red dots) |
| `validation/query_<id>/impl_runtime_ms` | Per-query runtime bar chart (Bespoke) |
| `validation/query_<id>/duckdb_runtime_ms` | Per-query runtime bar chart (DuckDB baseline) |

## Section colours

`js/sections.js` maps `current_prompt_descriptor` strings to colours matching `_SPAN_PALETTE` in `plot_timeline.py`. The timeline chart background fills and the sidebar stage list both use this palette. Hovering a stage in the sidebar highlights the corresponding chart region and log entries.

## Adding a new metric

1. Emit it via `drain.emit({"my/metric": value}, step)` in Python.
2. Read it in the appropriate `update*` function — `js/cards.js` for header/KPI/prompts, `js/chart-timeline.js` / `js/chart-query.js` for the always-visible charts, `js/chart-dist.js` for the modal, `js/log.js` for log entries (follow the pattern of `data[s]['my/metric']`).
3. If it needs a new chart series, add a dataset in `initChart` / `initQueryChart` and populate it in the corresponding `update*` function.
