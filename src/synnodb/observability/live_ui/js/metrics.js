'use strict';

// Per-query runtime extraction and cumulative-speedup computation.
// A "benchmark runtime row" is a successful, optimised, non-trace validation
// at a given scale factor. We keep the latest impl/duck pair per query and
// derive the running-total speedup over the union of queries seen so far.

function getRuntimeColumns(row) {
  const columns = new Map(); // qid -> {duckCol, implCol}
  for (const key of Object.keys(row)) {
    // The implementation runtime column was renamed impl_ -> bespoke_; accept
    // both so old and current runs render speedups.
    const match = key.match(/^validation\/query_(.+?)\/(duckdb_runtime_ms|impl_runtime_ms|bespoke_runtime_ms)$/);
    if (!match) continue;
    const qid = normalizeQueryId(match[1]);
    if (!columns.has(qid)) columns.set(qid, {});
    if (match[2] === 'duckdb_runtime_ms') columns.get(qid).duckCol = key;
    else                                  columns.get(qid).implCol = key;
  }
  return columns;
}

// The scale factor comes from the exec-settings dataclass, which is logged via
// prefix_dict("validation/") and therefore lands under validation/_scale_factor.
// Older runs used validation/scale_factor — accept both.
function getRowScaleFactor(row) {
  const raw = row['validation/_scale_factor'] ?? row['validation/scale_factor'];
  return Number(raw);
}

// ── Incremental row cache ────────────────────────────────────────────────
// Everything this module derives starts from the same per-row parse: the
// runtime columns (a regex over every key of the row), the scale factor, the
// benchmark-row predicate, and the executed query ids. Recomputing that for
// all rows on every render made each poll O(steps × keys) several times over.
// Rows are immutable once ingested — the incremental poll only ever re-sends
// the newest step while its turn is still accumulating — so we parse each row
// once and reuse the result.
//
// The cache is positional: entry i describes steps[i] of the store the caller
// passes in. The final index of the live list is never committed (its row can
// still gain fields); it is re-parsed on every access. Time-travel hands us a
// strict prefix of the same store, which reads the same committed entries.
// A generation reset or source switch replaces the row objects wholesale, so
// spot-checking the identity of the first and last committed row detects every
// divergence (steps restart at 0 after a reset, so step ids alone would not).
const _rowCache = {keys: [], rows: [], infos: [], size: 0};

function _resetMetricsCaches() {
  _rowCache.keys.length = 0;
  _rowCache.rows.length = 0;
  _rowCache.infos.length = 0;
  _rowCache.size = 0;
  _speedupCache.sf = undefined;
  _speedupCache.entries = [];
  _speedupCache.runtimes = new Map();
  _speedupCache.expected = new Set();
}

function _computeRowInfo(row) {
  const cols = getRuntimeColumns(row);
  const sf = getRowScaleFactor(row);
  const bench = (row.type || '').toLowerCase() === 'validate'
    && isMetricTrue(row['validation/compile_with_optimize'])
    && isMetricFalse(row['validation/trace_mode'])
    && isMetricFalse(row['validation/skip_validate'])
    && isMetricTrue(row['validation/correct'])
    && Number.isFinite(sf)
    && cols.size > 0;
  return {cols, sf, bench, queryIds: parseQueryIds(row['validation/query_ids_executed'])};
}

// Sync the cache against the caller's (steps, data) and commit every row that
// is final (all but the last of the given list). Returns nothing; afterwards
// _rowInfoAt serves committed entries from the cache and parses the rest fresh.
function _ingestRows(steps, data) {
  const c = _rowCache;
  const upTo = Math.min(c.size, steps.length);
  if (upTo > 0 &&
      (c.keys[0] !== String(steps[0]) || c.rows[0] !== data[steps[0]] ||
       c.keys[upTo - 1] !== String(steps[upTo - 1]) || c.rows[upTo - 1] !== data[steps[upTo - 1]])) {
    _resetMetricsCaches();
  }
  for (let i = c.size; i < steps.length - 1; i++) {
    c.keys.push(String(steps[i]));
    c.rows.push(data[steps[i]]);
    c.infos.push(_computeRowInfo(data[steps[i]] || {}));
  }
  c.size = Math.max(c.size, steps.length - 1);
}

function _rowInfoAt(steps, data, i) {
  return i < _rowCache.size ? _rowCache.infos[i] : _computeRowInfo(data[steps[i]] || {});
}

function _matchesSf(info, targetSf) {
  return info.bench && (targetSf == null || Math.abs(info.sf - targetSf) < 1e-12);
}

function getMaxScaleFactor(steps, data) {
  _ingestRows(steps, data);
  let maxSf = null;
  for (let i = 0; i < steps.length; i++) {
    const info = _rowInfoAt(steps, data, i);
    if (!info.bench) continue;
    maxSf = maxSf == null ? info.sf : Math.max(maxSf, info.sf);
  }
  return maxSf;
}

function getAvailableScaleFactors(steps, data) {
  _ingestRows(steps, data);
  const sfs = new Set();
  for (let i = 0; i < steps.length; i++) {
    const info = _rowInfoAt(steps, data, i);
    if (info.bench) sfs.add(info.sf);
  }
  return [...sfs].sort((a, b) => a - b);
}

// The SF whose runtimes drive the query bars and the timeline speedup line.
// Honor the user pick if it still exists in the data; otherwise fall back to
// the largest SF observed (so the view stays useful as the benchmark scales up).
function getEffectiveScaleFactor(steps, data) {
  if (selectedScaleFactor != null) {
    const sfs = getAvailableScaleFactors(steps, data);
    if (sfs.some(sf => Math.abs(sf - selectedScaleFactor) < 1e-12)) {
      return selectedScaleFactor;
    }
  }
  return getMaxScaleFactor(steps, data);
}

// ── Cumulative speedup series ────────────────────────────────────────────
// Cumulative cross-query speedup at each step. Pinned to the effective scale
// factor (user pick, or largest observed) so the line stays consistent.
//
// Each entry is {value, complete, nQueries, total}: `value` is the speedup (or
// null when we lack runtimes for every query seen so far). `total` is the full
// set of benchmark queries the run ever covers, and `nQueries` is how many this
// point covers. A point is preliminary (drawn dashed) while it covers fewer
// than `total` queries, and final (solid) once it covers them all.
//
// The walk accumulates left to right, so committed steps never change their
// {value, nQueries} once written: we keep the accumulator state (latest
// runtimes per query, union of queries seen) alongside the committed entries
// and only walk the new tail on each render. The accumulating final row is
// evaluated against copies so nothing provisional is committed. The whole
// cache is keyed on the target scale factor: when it changes (a larger SF
// appears, or the user picks one), the series is rebuilt from scratch.
const _speedupCache = {
  sf: undefined,          // targetSf the committed entries were built at
  entries: [],            // committed {value, nQueries}, entry i for steps[i]
  runtimes: new Map(),    // qid -> {impl, duck} after the committed rows
  expected: new Set(),    // union of benchmark queries after the committed rows
};

// Fold steps[i] into the accumulator state (latest runtimes + expected set).
function _applySpeedupRow(runtimes, expected, steps, data, i, targetSf) {
  const info = _rowInfoAt(steps, data, i);
  const row = data[steps[i]] || {};
  for (const qid of info.queryIds) {
    const cols = info.cols.get(qid);
    if (cols?.duckCol && cols?.implCol) expected.add(qid);
  }
  if (!_matchesSf(info, targetSf)) return;
  for (const [qid, cols] of info.cols) {
    if (!cols.duckCol || !cols.implCol) continue;
    const impl = Number(row[cols.implCol]);
    const duck = Number(row[cols.duckCol]);
    if (!Number.isFinite(impl) || !Number.isFinite(duck)) continue;
    runtimes.set(qid, {impl, duck});
    expected.add(qid);
  }
}

// The speedup entry for the current accumulator state.
function _speedupEntry(runtimes, expected) {
  if (!expected.size) return {value: null, nQueries: 0};
  let totalImpl = 0, totalDuck = 0, haveAll = true;
  for (const qid of expected) {
    const r = runtimes.get(qid);
    if (!r) { haveAll = false; break; }
    totalImpl += r.impl;
    totalDuck += r.duck;
  }
  return {value: haveAll && totalImpl > 0 ? totalDuck / totalImpl : null, nQueries: expected.size};
}

function computeSpeedupSeries(steps, data) {
  _ingestRows(steps, data);
  const targetSf = getEffectiveScaleFactor(steps, data);
  const sc = _speedupCache;
  if (!Object.is(sc.sf, targetSf)) {
    sc.sf = targetSf;
    sc.entries = [];
    sc.runtimes = new Map();
    sc.expected = new Set();
  }

  const n = steps.length;
  const out = sc.entries.slice(0, Math.min(sc.entries.length, n));
  for (let i = sc.entries.length; i < n; i++) {
    if (i < _rowCache.size) {
      // Final row: fold it into the committed state and keep its entry.
      _applySpeedupRow(sc.runtimes, sc.expected, steps, data, i, targetSf);
      sc.entries.push(_speedupEntry(sc.runtimes, sc.expected));
      out.push(sc.entries[i]);
    } else {
      // The still-accumulating final row: evaluate against copies so a later
      // render re-folds its (possibly grown) fields from the committed state.
      const runtimes = new Map(sc.runtimes);
      const expected = new Set(sc.expected);
      _applySpeedupRow(runtimes, expected, steps, data, i, targetSf);
      out.push(_speedupEntry(runtimes, expected));
    }
  }

  // The full benchmark suite for this run is the largest set of queries we ever
  // accumulate (expectedQueries grows monotonically, so this is its final size).
  // Any point covering fewer queries than that is preliminary. Derived purely
  // from the runtimes already logged — no dedicated "total queries" metric.
  const total = out.reduce((m, s) => Math.max(m, s.nQueries), 0) || null;
  return out.map(s => ({
    value: s.value,
    nQueries: s.nQueries,
    total,
    complete: s.value != null && (total == null || s.nQueries >= total),
  }));
}

// Latest impl/duck per query across all steps (sorted numerically when ids are integers).
function getQueryRuntimes(steps, data) {
  _ingestRows(steps, data);
  const targetSf = getEffectiveScaleFactor(steps, data);
  const map = new Map(); // id -> {duck, impl}
  for (let i = 0; i < steps.length; i++) {
    const info = _rowInfoAt(steps, data, i);
    if (!_matchesSf(info, targetSf)) continue;
    const d = data[steps[i]] || {};
    for (const [qid, cols] of info.cols) {
      if (!map.has(qid)) map.set(qid, {duck: null, impl: null});
      const row = map.get(qid);
      const duck = cols.duckCol != null ? d[cols.duckCol] : null;
      const impl = cols.implCol != null ? d[cols.implCol] : null;
      if (duck != null) row.duck = +duck;
      if (impl != null) row.impl = +impl;
    }
  }
  return [...map.entries()]
    .sort(([a], [b]) => {
      const an = Number(a), bn = Number(b);
      if (Number.isFinite(an) && Number.isFinite(bn)) return an - bn;
      return String(a).localeCompare(String(b));
    })
    .map(([id, r]) => ({id, duck: r.duck, impl: r.impl}));
}

// A resolved serving thread count is always >= 1; anything else (missing key,
// non-numeric) reads as "unknown" -> null.
function readThreadCount(row, key) {
  const v = Number(row[key]);
  return Number.isFinite(v) && v >= 1 ? v : null;
}

// Runs logged before the per-engine thread metrics existed only carry the shared
// core set: parallelism=false -> 1 thread, otherwise the number of pinned cores.
function sharedThreadCountFromCoreIds(row) {
  if (isMetricFalse(row['validation/parallelism'])) return 1;
  const coreIds = parseJsonField(row['validation/core_ids']);
  if (Array.isArray(coreIds) && coreIds.length) return coreIds.length;
  return null;
}

// Thread counts the DuckDB baseline and the bespoke (SynnoDB) engine were
// benchmarked at, from the latest benchmark row at the effective scale factor.
// Both engines run at the same resolved serving thread count, so these normally
// agree; a mismatch means the two runtimes are not directly comparable and is
// surfaced in the panel. Falls back to the shared core_ids/parallelism config
// for runs predating the explicit per-engine metrics.
function getThreadCounts(steps, data) {
  _ingestRows(steps, data);
  const targetSf = getEffectiveScaleFactor(steps, data);
  let result = null;
  for (let i = 0; i < steps.length; i++) {
    const info = _rowInfoAt(steps, data, i);
    if (!_matchesSf(info, targetSf)) continue;
    const row = data[steps[i]] || {};
    const duckdb = readThreadCount(row, 'validation/duckdb_num_threads');
    const bespoke = readThreadCount(row, 'validation/bespoke_num_threads');
    if (duckdb != null || bespoke != null) {
      result = {duckdb, bespoke};
      continue;
    }
    const shared = sharedThreadCountFromCoreIds(row);
    if (shared != null) result = {duckdb: shared, bespoke: shared};
  }
  return result;
}

function getQueryAxisMax(queries, isSpeedup) {
  const values = [];
  for (const q of queries) {
    if (isSpeedup) {
      if (q.duck != null && q.impl != null && q.impl > 0) values.push(q.duck / q.impl);
    } else {
      if (q.impl != null) values.push(q.impl);
      if (q.duck != null) values.push(q.duck);
    }
  }
  if (isSpeedup) values.push(1);
  const max = Math.max(...values.filter(Number.isFinite));
  return Number.isFinite(max) ? max : undefined;
}
