'use strict';

// Per-query runtime extraction and cumulative-speedup computation.
// A "benchmark runtime row" is a successful, optimised, non-trace validation
// at a given scale factor. We keep the latest impl/duck pair per query and
// derive the running-total speedup over the union of queries seen so far.

function getRuntimeColumns(row) {
  const columns = new Map(); // qid -> {duckCol, implCol}
  for (const key of Object.keys(row)) {
    const match = key.match(/^validation\/query_(.+?)\/(duckdb_runtime_ms|impl_runtime_ms)$/);
    if (!match) continue;
    const qid = normalizeQueryId(match[1]);
    if (!columns.has(qid)) columns.set(qid, {});
    if (match[2] === 'duckdb_runtime_ms') columns.get(qid).duckCol = key;
    else                                  columns.get(qid).implCol = key;
  }
  return columns;
}

function isBenchmarkRuntimeRow(row, scaleFactor = null) {
  if ((row.type || '').toLowerCase() !== 'validate') return false;
  if (!isMetricTrue(row['validation/compile_with_optimize'])) return false;
  if (!isMetricFalse(row['validation/trace_mode'])) return false;
  if (!isMetricFalse(row['validation/skip_validate'])) return false;
  if (!isMetricTrue(row['validation/correct'])) return false;
  const sf = Number(row['validation/scale_factor']);
  if (!Number.isFinite(sf)) return false;
  if (scaleFactor != null && Math.abs(sf - scaleFactor) >= 1e-12) return false;
  return getRuntimeColumns(row).size > 0;
}

function getMaxScaleFactor(steps, data) {
  let maxSf = null;
  for (const step of steps) {
    const row = data[step] || {};
    if (!isBenchmarkRuntimeRow(row)) continue;
    const sf = Number(row['validation/scale_factor']);
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
    const sf = Number(row['validation/scale_factor']);
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
function computeSpeedupSeries(steps, data) {
  const targetSf = getEffectiveScaleFactor(steps, data);
  const currentRuntimes = new Map(); // qid -> {impl, duck}
  const expectedQueries = new Set();
  const speedup = [];

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

    if (!expectedQueries.size) { speedup.push(null); continue; }

    let totalImpl = 0, totalDuck = 0, haveAll = true;
    for (const qid of expectedQueries) {
      const runtimes = currentRuntimes.get(qid);
      if (!runtimes) { haveAll = false; break; }
      totalImpl += runtimes.impl;
      totalDuck += runtimes.duck;
    }
    speedup.push(haveAll && totalImpl > 0 ? totalDuck / totalImpl : null);
  }
  return speedup;
}

// Latest impl/duck per query across all steps (sorted numerically when ids are integers).
function getQueryRuntimes(steps, data) {
  const targetSf = getEffectiveScaleFactor(steps, data);
  const map = new Map(); // id -> {duck, impl}
  for (const s of steps) {
    const d = data[s] || {};
    if (!isBenchmarkRuntimeRow(d, targetSf)) continue;
    for (const k of Object.keys(d)) {
      const m = k.match(/^validation\/query_(.+?)\/(duckdb|impl)_runtime_ms$/);
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
