'use strict';

// ── Formatting ───────────────────────────────────────────────────────────
const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

function fmtCost(v) { return v == null ? '—' : '$' + (+v).toFixed(4); }

function fmtTime(s) {
  if (s == null) return '—';
  s = +s;
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = Math.round(s%60);
  return h > 0 ? h+'h '+m+'m' : m > 0 ? m+'m '+sec+'s' : sec+'s';
}

function fmtNum(v) { return v == null ? '—' : (+v).toLocaleString(); }

function fmtPieTime(s) {
  s = +s;
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = Math.round(s%60);
  return h > 0 ? h+'h '+m+'m' : m > 0 ? m+'m '+sec+'s' : sec+'s';
}

function fmtTimelineTick(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return '';
  if (seconds >= 3600) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return `${h}h ${m}m`;
  }
  if (seconds >= 60) {
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return `${m}m ${String(s).padStart(2, '0')}s`;
  }
  return `${Math.round(seconds)}s`;
}

// ── Field parsers ────────────────────────────────────────────────────────
function parseJsonField(v) {
  if (v == null) return null;
  if (typeof v !== 'string') return v;
  try { return JSON.parse(v); } catch { return v; }
}

function normalizeQueryId(rawQueryId) {
  const q = String(rawQueryId);
  return /^\d+$/.test(q) ? String(Number(q)) : q;
}

function parseQueryIds(value) {
  if (value == null) return [];
  if (Array.isArray(value)) return value.map(normalizeQueryId);
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed)) return parsed.map(normalizeQueryId);
    } catch {}
  }
  return [];
}

function isMetricTrue(value)  { return value === true  || value === 1 || value === 'true'  || value === 'True'; }
function isMetricFalse(value) { return value === false || value === 0 || value === 'false' || value === 'False' || value == null; }

// ── Chart geometry helpers ───────────────────────────────────────────────
function addAxisHeadroom(axis, ratio = 0.12) {
  axis.min = 0;
  const max = Number.isFinite(axis.max) ? axis.max : 0;
  if (max <= 0) { axis.max = 1; return; }
  axis.max = max * (1 + ratio);
}

function getSegmentBounds(points, idx) {
  const point = points[idx];
  if (!point) return null;
  const prev = points[idx - 1];
  const next = points[idx + 1];
  const x = point.x;
  const left  = prev ? (prev.x + x) / 2 : x - ((next ? next.x - x : 1) / 2);
  const right = next ? (x + next.x) / 2 : x + ((prev ? x - prev.x : 1) / 2);
  return {
    left:  Math.min(left, right),
    right: Math.max(left, right),
  };
}
