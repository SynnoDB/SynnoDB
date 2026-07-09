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

function isBenchmarkRuntimeRow(row, scaleFactor = null) {
  if ((row.type || '').toLowerCase() !== 'validate') return false;
  if (!isMetricTrue(row['validation/compile_with_optimize'])) return false;
  if (!isMetricFalse(row['validation/trace_mode'])) return false;
  if (!isMetricFalse(row['validation/skip_validate'])) return false;
  if (!isMetricTrue(row['validation/correct'])) return false;
  const sf = getRowScaleFactor(row);
  if (!Number.isFinite(sf)) return false;
  if (scaleFactor != null && Math.abs(sf - scaleFactor) >= 1e-12) return false;
  return getRuntimeColumns(row).size > 0;
}

function getMaxScaleFactor(steps, data) {
  let maxSf = null;
  for (const step of steps) {
    const row = data[step] || {};
    if (!isBenchmarkRuntimeRow(row)) continue;
    const sf = getRowScaleFactor(row);
    if (!Number.isFinite(sf)) continue;
    maxSf = maxSf == null ? sf : Math.max(maxSf, sf);
  }
  return maxSf;
}

function getAvailableScaleFactors(steps, data) {
  const sfs = new Set();
  for (const step of steps) {
    const row = data[step] || {};
    if (!isBenchmarkRuntimeRow(row)) continue;
    const sf = getRowScaleFactor(row);
    if (Number.isFinite(sf)) sfs.add(sf);
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

// Cumulative cross-query speedup at each step. Pinned to the effective scale
// factor (user pick, or largest observed) so the line stays consistent.
//
// Each entry is {value, complete, nQueries, total}: `value` is the speedup (or
// null when we lack runtimes for every query seen so far). `total` is the full
// set of benchmark queries the run ever covers, and `nQueries` is how many this
// point covers. A point is preliminary (drawn dashed) while it covers fewer
// than `total` queries, and final (solid) once it covers them all.
function computeSpeedupSeries(steps, data) {
  const targetSf = getEffectiveScaleFactor(steps, data);
  const currentRuntimes = new Map(); // qid -> {impl, duck}
  const expectedQueries = new Set();
  const series = [];

  for (const step of steps) {
    const row = data[step] || {};
    const rowColumns = getRuntimeColumns(row);
    const rowQueryIds = parseQueryIds(row['validation/query_ids_executed']);

    for (const qid of rowQueryIds) {
      const cols = rowColumns.get(qid);
      if (cols?.duckCol && cols?.implCol) expectedQueries.add(qid);
    }

    if (isBenchmarkRuntimeRow(row, targetSf)) {
      for (const [qid, cols] of rowColumns.entries()) {
        if (!cols.duckCol || !cols.implCol) continue;
        const impl = Number(row[cols.implCol]);
        const duck = Number(row[cols.duckCol]);
        if (!Number.isFinite(impl) || !Number.isFinite(duck)) continue;
        currentRuntimes.set(qid, {impl, duck});
        expectedQueries.add(qid);
      }
    }

    if (!expectedQueries.size) {
      series.push({value: null, nQueries: 0});
      continue;
    }

    let totalImpl = 0, totalDuck = 0, haveAll = true;
    for (const qid of expectedQueries) {
      const runtimes = currentRuntimes.get(qid);
      if (!runtimes) { haveAll = false; break; }
      totalImpl += runtimes.impl;
      totalDuck += runtimes.duck;
    }
    const value = haveAll && totalImpl > 0 ? totalDuck / totalImpl : null;
    series.push({value, nQueries: expectedQueries.size});
  }

  // The full benchmark suite for this run is the largest set of queries we ever
  // accumulate (expectedQueries grows monotonically, so this is its final size).
  // Any point covering fewer queries than that is preliminary. Derived purely
  // from the runtimes already logged — no dedicated "total queries" metric.
  const total = series.reduce((m, s) => Math.max(m, s.nQueries), 0) || null;
  return series.map(s => ({
    value: s.value,
    nQueries: s.nQueries,
    total,
    complete: s.value != null && (total == null || s.nQueries >= total),
  }));
}

// Latest impl/duck per query across all steps (sorted numerically when ids are integers).
function getQueryRuntimes(steps, data) {
  const targetSf = getEffectiveScaleFactor(steps, data);
  const map = new Map(); // id -> {duck, impl}
  for (const s of steps) {
    const d = data[s] || {};
    if (!isBenchmarkRuntimeRow(d, targetSf)) continue;
    for (const k of Object.keys(d)) {
      const m = k.match(/^validation\/query_(.+?)\/(duckdb|impl|bespoke)_runtime_ms$/);
      if (!m) continue;
      const qid = normalizeQueryId(m[1]);
      if (!map.has(qid)) map.set(qid, {duck: null, impl: null});
      const row = map.get(qid);
      const val = d[k] != null ? +d[k] : null;
      if (val == null) continue;
      if (m[2] === 'duckdb') row.duck = val;
      else                   row.impl = val;
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
  const targetSf = getEffectiveScaleFactor(steps, data);
  let result = null;
  for (const s of steps) {
    const row = data[s] || {};
    if (!isBenchmarkRuntimeRow(row, targetSf)) continue;
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
