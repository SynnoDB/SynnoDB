'use strict';

// Activity log panel: one collapsible <details> per step, appended in order.
// We diff against existing entries and only append the new tail; if any
// existing entry no longer corresponds to a known step (e.g. after a source
// switch), the whole list is rebuilt.

const LOG_TYPE_META = {
  llm:        { label:'LLM',        cls:'lt-llm'      },
  apply_patch:{ label:'Patch',      cls:'lt-patch'     },
  shell:      { label:'Shell',      cls:'lt-shell'     },
  compile:    { label:'Compile',    cls:'lt-compile'   },
  validate:   { label:'RUN',        cls:'lt-validate'  },
  data_inspect:{ label:'Inspect',   cls:'lt-datainspect'},
  compaction: { label:'Compaction', cls:'lt-compaction'},
};

// All known log types, in badge order, plus the 'other' catch-all. Used by the
// type filter's chip set.
const LOG_TYPE_KEYS = [...Object.keys(LOG_TYPE_META), 'other'];

function logTypeKey(d) {
  const t = (d['type'] || 'other').toLowerCase();
  return LOG_TYPE_KEYS.includes(t) ? t : 'other';
}

// Whether a step ended in a failure. Only the step types that carry a real
// error signal can fail; the rest (llm, shell, compaction) have no failure
// concept and are treated as non-errors.
function logHasError(type, d) {
  if (type === 'compile')      return d['compile/error'] === true;
  if (type === 'data_inspect') return d['data_inspect/error'] === true;
  if (type === 'validate') {
    return d['validation/compile_error'] === true || d['validation/correct'] === false;
  }
  if (type === 'apply_patch') {
    const failed = parseJsonField(d['apply_patch/failed']);
    return (failed && failed.length > 0) || d['apply_patch/rejected'] === true;
  }
  return false;
}

// Activity-log filters, keyed by the data-filter attribute on their control.
// Two control kinds:
//   - 'segmented': tri-state, state is a string ('off' plus the filter's values).
//   - 'chips':     multi-select, state is a Set of the enabled values.
// `isActive(state)` reports whether the filter currently constrains anything;
// `match(d, state)` decides whether a step's raw data record passes. An entry is
// displayed only when every active filter passes.
const LOG_FILTERS = {
  type: {
    kind: 'chips',
    // Neutral (inactive) when every type is enabled. An empty selection is
    // active and legitimately hides everything (the "No activity matches"
    // placeholder then explains the blank panel).
    isActive(state) { return state.size < LOG_TYPE_KEYS.length; },
    match(d, state)  { return state.has(logTypeKey(d)); },
  },
  outcome: {
    kind: 'segmented',
    // 'errors' → only failed steps; 'success' → only steps that did not fail.
    isActive(state) { return state !== 'off'; },
    match(d, state) {
      const err = logHasError(logTypeKey(d), d);
      return state === 'errors' ? err : !err;
    },
  },
  cached: {
    kind: 'segmented',
    isActive(state) { return state !== 'off'; },
    // 'yes' → only entries served from cache; 'no' → only entries that were not.
    // Each cache-capable step type reports its own cache signal; a step is
    // "from cache" if any of them says so.
    match(d, state) {
      const fromCache = d['answered_from_cache'] === true            // llm
        || d['data_inspect/cached'] === true                        // data_inspect
        || d['shell/cached'] === true                               // shell
        || d['validation/replayed_from_cache'] === true             // validate
        || d['compile/cached'] === true                             // compile
        || d['apply_patch/cached'] === true                         // apply_patch
        || d['compaction/cached'] === true;                         // compaction
      return state === 'yes' ? fromCache : !fromCache;
    },
  },
};
const _logFilterState = {
  type: new Set(LOG_TYPE_KEYS),
  outcome: 'off',
  cached: 'off',
};

function logEntryPasses(d) {
  for (const id in LOG_FILTERS) {
    const f = LOG_FILTERS[id];
    const state = _logFilterState[id];
    if (f.isActive(state) && !f.match(d, state)) return false;
  }
  return true;
}

function activeLogFilterCount() {
  let n = 0;
  for (const id in LOG_FILTERS) if (LOG_FILTERS[id].isActive(_logFilterState[id])) n++;
  return n;
}

// Body text is long enough to be worth a dedicated viewer window rather than
// the cramped, fixed-height .log-body scroll box.
const LOG_DETAIL_THRESHOLD = 500;

// step -> raw (unescaped) body text, for the "View full output" modal. Kept
// out of the DOM (rather than a data-attribute) since bodies can be tens of
// thousands of characters.
let _logBodyText = new Map();

function logTruncated(type, d) {
  if (type === 'llm') return !!d['llm/output_truncated'];
  if (type === 'apply_patch') return !!d['apply_patch/truncated'];
  if (type === 'shell') return !!d['shell/truncated'];
  if (type === 'data_inspect') return !!d['data_inspect/truncated'];
  return false;
}

function logDesc(type, d) {
  if (type === 'llm') {
    const parts = [d['current_prompt_descriptor'], d['agent_name']].filter(Boolean);
    return parts.join(' · ') || 'LLM call';
  }
  if (type === 'apply_patch') {
    const files   = parseJsonField(d['apply_patch/files']);
    const added   = d['apply_patch/added_loc_count'];
    const deleted = d['apply_patch/deleted_loc_count'];
    const failed  = parseJsonField(d['apply_patch/failed']);
    const rejected = d['apply_patch/rejected'] === true;
    const hasFailed = failed && failed.length;
    const names = (files && files.length) ? files.map(f => f.split('/').pop()) : [];
    const who = names.length
      ? names.slice(0,3).join(', ') + (names.length > 3 ? ' +' + (names.length-3) : '')
      : 'code change';
    const cachedStr = d['apply_patch/cached'] ? ' · cached' : '';
    // A rejected call never reached the editor (invalid args), so it is not a real
    // edit - label it as such instead of a bare +0/-0 step. It still carries a cache
    // signal (it replays from cache when the LLM turn that produced it does).
    if (rejected) return who + ' ⚠ invalid args (rejected)' + cachedStr;
    const failedStr = hasFailed ? ' ⚠ ' + failed.length + ' failed' : '';
    const delta = (!hasFailed && (added != null || deleted != null))
      ? ' (+' + (added||0) + '/-' + (deleted||0) + ')' : '';
    return who + delta + failedStr + cachedStr;
  }
  if (type === 'shell') {
    const cmds = parseJsonField(d['shell/commands']);
    if (cmds && cmds.length) {
      const first = String(cmds[0]).trim();
      const suffix = cmds.length > 1 ? ' (+' + (cmds.length-1) + ')' : '';
      return first.length > 60 ? first.slice(0,58) + '…' + suffix : first + suffix;
    }
    return 'shell command';
  }
  if (type === 'compile') {
    return d['compile/error'] ? 'error' : 'success';
  }
  if (type === 'data_inspect') {
    const status = d['data_inspect/status'] || (d['data_inspect/error'] ? 'error' : 'ok');
    const cached = d['data_inspect/cached'] ? ' · cached' : '';
    const sql = String(d['data_inspect/sql'] || '').replace(/\s+/g, ' ').trim();
    const q = sql.length > 48 ? sql.slice(0, 46) + '…' : sql;
    return [status, q].filter(Boolean).join(' · ') + cached;
  }
  if (type === 'validate') {
    const mode = d['validation/run_mode'];
    const modeStr = mode ? String(mode) : null;
    if (d['validation/compile_error']) return [modeStr, 'compile error'].filter(Boolean).join(' · ');
    const queries = parseJsonField(d['validation/query_ids_executed']);
    const trace = d['validation/trace_mode'];
    const qStr = queries && queries.length ? queries.join(', ') : null;
    const tStr = trace ? 'trace' : null;
    const c = d['validation/correct'];
    const result = c === true ? 'correct ✓' : c === false ? 'incorrect' : 'ran';
    return [modeStr, qStr, result, tStr].filter(Boolean).join(' · ');
  }
  if (type === 'compaction') {
    const items = d['compaction/output_items'];
    const itemsStr = items != null ? items + ' items' : null;
    const cachedStr = d['compaction/cached'] ? 'cached' : null;
    return [itemsStr, cachedStr].filter(Boolean).join(' · ') || 'compaction';
  }
  return d['agent_name'] || type;
}

function logBody(type, d) {
  if (type === 'llm') {
    const out = d['llm/output_text'];
    return (out && out.trim()) ? out : '(no text output)';
  }
  if (type === 'apply_patch') {
    const parts = [];
    const failed = parseJsonField(d['apply_patch/failed']);
    if (failed && failed.length) parts.push('FAILED:\n' + failed.join('\n'));
    const s = d['apply_patch/string'];
    if (s && s.trim()) parts.push(s);
    else {
      const files = parseJsonField(d['apply_patch/files']);
      if (files) parts.push(JSON.stringify(files, null, 2));
    }
    return parts.join('\n\n') || '(no diff)';
  }
  if (type === 'shell') {
    const cmds = parseJsonField(d['shell/commands']);
    const out  = d['shell/outputs'];
    const parts = [];
    if (cmds && cmds.length) parts.push('$ ' + cmds.join('\n$ '));
    if (out && out.trim())   parts.push(out);
    return parts.join('\n\n') || '(no output)';
  }
  if (type === 'data_inspect') {
    const sql = d['data_inspect/sql'];
    const out = d['data_inspect/output'];
    const parts = [];
    if (sql && sql.trim()) parts.push('SQL:\n' + sql);
    if (out && out.trim()) parts.push('Result:\n' + out);
    return parts.join('\n\n') || '(no output)';
  }
  const skip = new Set(['type','turn','prompt_idx','agent_name','current_prompt','current_prompt_descriptor']);
  const lines = [];
  const entries = type === 'validate'
    ? Object.entries(d).sort(([a], [b]) => a.localeCompare(b))
    : Object.entries(d);
  for (const [k, v] of entries) {
    if (skip.has(k) || v == null) continue;
    lines.push(k + ': ' + (typeof v === 'object' ? JSON.stringify(v) : v));
  }
  return lines.join('\n') || '(no details)';
}

function logDuration(steps, data, idx) {
  const rowRuntime = Number((data[steps[idx]] || {})['total/runtime']);
  if (!Number.isFinite(rowRuntime)) return null;

  let prevRuntime = 0;
  for (let i = idx - 1; i >= 0; i--) {
    const candidate = Number((data[steps[i]] || {})['total/runtime']);
    if (Number.isFinite(candidate)) {
      prevRuntime = candidate;
      break;
    }
  }

  return Math.max(0, rowRuntime - prevRuntime);
}

function logExpandedMeta(type, d, steps, data, idx) {
  const parts = ['Wall time ' + fmtTime(logDuration(steps, data, idx))];
  if (type === 'llm') {
    parts.push('Cost ' + fmtCost(d['cost_usd']));
    parts.push('Input tokens ' + fmtNum(d['input_tokens']));
  }
  return parts.join(' · ');
}

// ── Virtualized activity log ─────────────────────────────────────────────
// A run can accumulate many thousands of steps. Rendering one <details> node
// (each holding a full <pre> body) per step buries the DOM under tens of
// thousands of nodes and megabytes of text, which makes scrolling and every
// subsequent poll crawl. Instead we keep all entries as lightweight metadata
// and mount only the handful of rows inside (and just around) the viewport,
// absolutely positioned inside a spacer sized to the full list height. Bodies
// are mounted lazily on expand, so the bulk of the text never enters the DOM.

// Ordered step ids (as strings) currently ingested, mirroring the steps array.
let _logOrder = [];
// step -> precomputed render metadata {step, metaLabel, metaCls, desc,
// expandedMeta, passes}. Body text is resolved lazily on expand (_logBodyText).
let _logRows = new Map();
// Subset of _logOrder that passes the active filters, in order — the domain the
// virtualizer scrolls over. _logVisibleIndex maps step -> its index here.
let _logVisible = [];
let _logVisibleIndex = new Map();
// Steps the user has expanded. Persists across mount/unmount so a row that
// scrolls out and back keeps its open state.
let _logExpanded = new Set();
// Measured pixel height per expanded step (collapsed rows use _logCollapsedH).
let _logHeights = new Map();
// step -> mounted <details> element for the rows currently in the window.
let _logMounted = new Map();
// Prefix offsets: _logOffsets[i] is the top (px) of visible row i; the spacer's
// total height is the sum of every visible row's height.
let _logOffsets = [];
let _logTotalHeight = 0;
// Section-hover highlight range, applied to mounted rows as they render.
let _logHoverActive = false;
let _logHoverFirst = null;
let _logHoverLast = null;
let _logScrollRaf = 0;

// Collapsed row height is read from CSS via a live measurement (it self-corrects
// on the first real layout); these are the initial estimates.
let _logCollapsedH = 30;
const LOG_EXPANDED_ESTIMATE = 160;  // provisional height until a row is measured
const LOG_OVERSCAN = 8;             // rows rendered beyond each viewport edge

function _logList() { return document.getElementById('log-list'); }

// The relative spacer that holds the absolutely-positioned window rows. Created
// once; the "no activity" / "no match" placeholder is a sibling after it.
function _logViewport() {
  const list = _logList();
  let vp = document.getElementById('log-viewport');
  if (!vp) {
    list.innerHTML = '';
    vp = document.createElement('div');
    vp.id = 'log-viewport';
    list.appendChild(vp);
  }
  return vp;
}

function _logRowHeight(step) {
  return _logExpanded.has(step)
    ? (_logHeights.get(step) || LOG_EXPANDED_ESTIMATE)
    : _logCollapsedH;
}

function _logRebuildVisible() {
  _logVisible = _logOrder.filter(s => _logRows.get(s).passes);
  _logVisibleIndex = new Map(_logVisible.map((s, i) => [s, i]));
}

function _logRebuildOffsets() {
  _logOffsets = new Array(_logVisible.length);
  let acc = 0;
  for (let i = 0; i < _logVisible.length; i++) {
    _logOffsets[i] = acc;
    acc += _logRowHeight(_logVisible[i]);
  }
  _logTotalHeight = acc;
  _logViewport().style.height = _logTotalHeight + 'px';
}

// Largest visible index whose top is <= y (binary search over _logOffsets).
function _logIndexAt(y) {
  let lo = 0, hi = _logOffsets.length - 1, ans = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (_logOffsets[mid] <= y) { ans = mid; lo = mid + 1; }
    else hi = mid - 1;
  }
  return ans;
}

function _logSummaryHtml(row) {
  return `<summary>
      <span class="log-type ${row.metaCls}">${esc(row.metaLabel)}</span>
      <span class="log-desc">${esc(row.desc)}</span>
      <span class="log-turn">#${row.step}</span>
      <span class="log-chevron">&#9654;</span>
    </summary>`;
}

// Heavy per-step fields (bodies, prompt/config text, debug metadata) are
// stripped from /api/stats and fetched per step from /api/step_body (see the
// backend's _LAZY_FIELDS). Deduped and cached so every consumer — an expanding
// log row, its "view full" modal, the prompt modal — shares one request per
// step. Cleared on source switch along with the rest of the log state.
let _stepFieldsCache = new Map();  // step -> Promise<fields | null>

function fetchStepFields(step) {
  const key = String(step);
  if (_stepFieldsCache.has(key)) return _stepFieldsCache.get(key);
  const p = (async () => {
    try {
      const r = await fetch('/api/step_body?step=' + encodeURIComponent(key));
      if (r.ok) return (await r.json()).fields || {};
    } catch (_) { /* unavailable — callers fall back to the snapshot fields */ }
    // Failure (404 on a non-stripping source, or a transient hiccup) is not
    // memoized: evicting lets the next consumer retry instead of pinning the
    // degraded fallback for the rest of the session.
    _stepFieldsCache.delete(key);
    return null;
  })();
  _stepFieldsCache.set(key, p);
  return p;
}

// The log types whose body text lives in a lazily-served field, and the field
// each one carries. A snapshot that still inlines the body (a source that does
// not strip) is used directly — no round-trip. Every other type's expanded view
// dumps the step's full field set, which includes lazily-served debug fields,
// so it fetches /api/step_body on first expand too.
const LOG_BODY_TYPES = new Set(['llm', 'shell', 'data_inspect', 'apply_patch']);
const LOG_BODY_FIELD_BY_TYPE = {
  llm: 'llm/output_text',
  shell: 'shell/outputs',
  data_inspect: 'data_inspect/output',
  apply_patch: 'apply_patch/string',
};
// step -> in-flight body fetch, so a row mounting and its modal opening share one
// request. Fetched bodies land in _logBodyText (the render/modal cache).
let _logBodyPending = new Map();

function _hasInlineBody(type, d) {
  const f = LOG_BODY_FIELD_BY_TYPE[type];
  return f != null && d[f] != null;
}

// The body text for a step if it can be produced without a server round-trip —
// a snapshot that still inlines the body. Returns null when /api/step_body is
// needed. Caches whatever it resolves.
function _logResolveInlineBody(step) {
  if (_logBodyText.has(step)) return _logBodyText.get(step);
  const d = (_lastData && _lastData[step]) || {};
  const type = (d['type'] || 'other').toLowerCase();
  if (LOG_BODY_TYPES.has(type) && _hasInlineBody(type, d)) {
    const body = logBody(type, d);
    _logBodyText.set(step, body);
    return body;
  }
  return null;
}

// Resolve a step's body text, fetching its lazily-served fields when they were
// stripped from the snapshot. Deduped and cached.
function fetchLogBody(step) {
  const inline = _logResolveInlineBody(step);
  if (inline != null) return Promise.resolve(inline);
  if (_logBodyPending.has(step)) return _logBodyPending.get(step);
  const d = (_lastData && _lastData[step]) || {};
  const type = (d['type'] || 'other').toLowerCase();
  const p = fetchStepFields(step).then(fields => {
    const body = logBody(type, fields ? Object.assign({}, d, fields) : d);
    _logBodyText.set(step, body);
    _logBodyPending.delete(step);
    return body;
  });
  _logBodyPending.set(step, p);
  return p;
}

function _logRowInner(row, expanded) {
  if (!expanded) return _logSummaryHtml(row);
  const body = _logResolveInlineBody(row.step);
  if (body == null) {
    // Body not loaded yet — show a spinner; _logLoadBody re-renders on arrival.
    return _logSummaryHtml(row) + `<div class="log-body log-body-loading">Loading…</div>`;
  }
  const viewFullBtn = body.length > LOG_DETAIL_THRESHOLD
    ? `<button class="log-view-full-btn" type="button" data-step="${row.step}">View full output</button>`
    : '';
  return _logSummaryHtml(row) +
    `<div class="log-body"><div class="log-expanded-meta">${esc(row.expandedMeta)}</div>${viewFullBtn}<pre>${esc(body)}</pre></div>`;
}

// Fetch an expanded row's body and re-render it in place once it arrives.
function _logLoadBody(step) {
  fetchLogBody(step).then(() => {
    const el = _logMounted.get(step);
    if (!el || !_logExpanded.has(step)) return;  // scrolled away or collapsed meanwhile
    el.innerHTML = _logRowInner(_logRows.get(step), true);
    _logHeights.delete(step);
    _logRebuildOffsets();
    _logRenderWindow();
  });
}

function _logBuildRow(row) {
  const expanded = _logExpanded.has(row.step);
  const el = document.createElement('details');
  el.className = 'log-entry';
  el.dataset.step = row.step;
  if (expanded) el.open = true;
  const n = +row.step;
  if (_logHoverActive && _logHoverFirst != null && n >= _logHoverFirst && n <= _logHoverLast) {
    el.classList.add('log-highlighted');
  }
  el.innerHTML = _logRowInner(row, expanded);
  if (expanded && !_logBodyText.has(row.step)) _logLoadBody(row.step);
  return el;
}

// Mount exactly the window [start-overscan, end+overscan] of visible rows,
// reusing already-mounted nodes so scrolling doesn't churn the DOM.
function _logRenderWindow() {
  const vp = _logViewport();
  const total = _logVisible.length;
  if (!total) {
    for (const [, el] of _logMounted) el.remove();
    _logMounted.clear();
    return;
  }
  const list = _logList();
  const scrollTop = list.scrollTop;
  const clientH = list.clientHeight || 400;
  let start = Math.max(0, _logIndexAt(scrollTop) - LOG_OVERSCAN);
  let end = Math.min(total - 1, _logIndexAt(scrollTop + clientH) + LOG_OVERSCAN);

  const need = new Set();
  for (let i = start; i <= end; i++) need.add(_logVisible[i]);
  for (const [step, el] of _logMounted) {
    if (!need.has(step)) { el.remove(); _logMounted.delete(step); }
  }
  for (let i = start; i <= end; i++) {
    const step = _logVisible[i];
    let el = _logMounted.get(step);
    if (!el) { el = _logBuildRow(_logRows.get(step)); vp.appendChild(el); _logMounted.set(step, el); }
    el.style.top = _logOffsets[i] + 'px';
  }
  _logMeasure();
}

// Correct row heights against reality: learn the true collapsed height once, and
// record each expanded row's measured height. When anything changed, rebuild the
// offsets and reposition the mounted rows (no re-mounting needed).
function _logMeasure() {
  let dirty = false;
  for (const [step, el] of _logMounted) {
    const h = el.offsetHeight;
    if (!h) continue;  // panel hidden — measurement not meaningful yet
    if (_logExpanded.has(step)) {
      if (_logHeights.get(step) !== h) { _logHeights.set(step, h); dirty = true; }
    } else if (Math.abs(h - _logCollapsedH) > 0.5) {
      _logCollapsedH = h; dirty = true;
    }
  }
  if (!dirty) return;
  _logRebuildOffsets();
  for (const [step, el] of _logMounted) {
    const i = _logVisibleIndex.get(step);
    if (i != null) el.style.top = _logOffsets[i] + 'px';
  }
}

function _logAtBottom() {
  const list = _logList();
  return list.scrollHeight - list.scrollTop - list.clientHeight < 40;
}

function _logScrollToBottom() {
  const list = _logList();
  // Two passes: the first render learns the true row heights (the collapsed
  // height starts as an estimate), which can change the total height; re-pin
  // afterwards so we land exactly at the bottom.
  list.scrollTop = list.scrollHeight;
  _logRenderWindow();
  list.scrollTop = list.scrollHeight;
  _logRenderWindow();
}

function _logReset() {
  _logOrder = [];
  _logRows.clear();
  _logVisible = [];
  _logVisibleIndex.clear();
  _logExpanded.clear();
  _logHeights.clear();
  _logBodyText.clear();
  _logBodyPending.clear();
  _stepFieldsCache.clear();
  for (const [, el] of _logMounted) el.remove();
  _logMounted.clear();
  _logOffsets = [];
  _logTotalHeight = 0;
  const vp = _logViewport();
  vp.style.height = '0px';
}

function _logComputeRow(step, idx, steps, data) {
  const d = data[step] || {};
  const type = (d['type'] || 'other').toLowerCase();
  const meta = LOG_TYPE_META[type] || { label: type.toUpperCase(), cls: 'lt-other' };
  // The body is not built here — it is resolved lazily on expand (and fetched
  // from /api/step_body when its text was stripped from the snapshot).
  return {
    step,
    metaLabel: meta.label,
    metaCls: meta.cls,
    desc: logDesc(type, d),
    expandedMeta: logExpandedMeta(type, d, steps, data, idx),
    passes: logEntryPasses(d),
  };
}

function updateLog(steps, data) {
  if (!steps.length) { _logReset(); updateLogPlaceholder(); return; }

  // Source switch / timeline reset: the store no longer contains a step we had
  // ingested. Drop everything and rebuild from the new payload.
  const stepsSet = new Set(steps.map(String));
  if (_logOrder.some(s => !stepsSet.has(s))) _logReset();

  const known = new Set(_logOrder);
  const wasAtBottom = _logAtBottom();
  let added = false;
  steps.forEach((s, idx) => {
    const key = String(s);
    if (known.has(key)) return;   // rendered rows are never re-rendered (matches prior behaviour)
    _logRows.set(key, _logComputeRow(key, idx, steps, data));
    _logOrder.push(key);
    added = true;
  });
  if (!added) return;

  _logRebuildVisible();
  _logRebuildOffsets();
  updateLogPlaceholder();
  if (wasAtBottom) _logScrollToBottom();
  else _logRenderWindow();
}

// ── Filter menu ──────────────────────────────────────────────────────────
// Re-evaluate every ingested entry against the active filters, rebuild the
// visible domain, and re-render the window. Called when the filter selection
// changes (new entries apply the predicate at ingest time in updateLog).
function applyLogFilters() {
  for (const step of _logOrder) {
    const d = (_lastData && _lastData[step]) || {};
    _logRows.get(step).passes = logEntryPasses(d);
  }
  _logRebuildVisible();
  _logRebuildOffsets();
  _logList().scrollTop = Math.min(_logList().scrollTop, _logTotalHeight);
  _logRenderWindow();
  updateLogPlaceholder();
}

// Show a placeholder when there is nothing to show — either no activity yet, or
// active filters hide every entry — so the panel never looks mysteriously blank.
function updateLogPlaceholder() {
  const list = _logList();
  let placeholder = list.querySelector('.log-empty');
  let text = null;
  if (!_logOrder.length) text = 'No activity yet…';
  else if (!_logVisible.length && activeLogFilterCount()) text = 'No activity matches the active filter.';

  if (text) {
    if (!placeholder) {
      placeholder = document.createElement('div');
      placeholder.className = 'log-empty';
      list.appendChild(placeholder);
    }
    placeholder.textContent = text;
  } else if (placeholder) {
    placeholder.remove();
  }
}

// Toggle a section's highlight across the mounted log rows. Called from
// setHoveredSection; the range is also applied to rows as they mount, so rows
// scrolled into view while a section is hovered pick up the highlight too.
function applyLogHover(first, last, active) {
  _logHoverActive = active;
  _logHoverFirst = first;
  _logHoverLast = last;
  for (const [step, el] of _logMounted) {
    const n = +step;
    el.classList.toggle('log-highlighted', active && first != null && n >= first && n <= last);
  }
}

// Expand/collapse is driven manually (not native <details> toggling) so the
// virtualizer stays the single source of truth for open state and row height.
(function initLogVirtualList() {
  const list = _logList();
  const vp = _logViewport();

  vp.addEventListener('click', e => {
    const summary = e.target.closest('summary');
    if (!summary) return;
    e.preventDefault();  // suppress native <details> toggle; we manage it
    const entry = summary.parentElement;
    const step = entry.dataset.step;
    const expanded = !_logExpanded.has(step);
    if (expanded) _logExpanded.add(step); else _logExpanded.delete(step);
    _logHeights.delete(step);  // force a fresh measurement at the new state
    entry.open = expanded;
    entry.innerHTML = _logRowInner(_logRows.get(step), expanded);
    if (expanded && !_logBodyText.has(step)) _logLoadBody(step);
    _logRebuildOffsets();
    _logRenderWindow();
  });

  list.addEventListener('scroll', () => {
    if (_logScrollRaf) return;
    _logScrollRaf = requestAnimationFrame(() => { _logScrollRaf = 0; _logRenderWindow(); });
  });

  // The panel's height changes when it opens/closes or the supervisor panel is
  // dragged; a size change (including 0 → visible) means a different window.
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(() => _logRenderWindow()).observe(list);
  }

  updateLogPlaceholder();  // restore the initial "No activity yet…" after wiring up
})();

(function initLogFilter() {
  const wrap  = document.getElementById('log-filter-wrap');
  const btn   = document.getElementById('log-filter-btn');
  const menu  = document.getElementById('log-filter-menu');
  const count = document.getElementById('log-filter-count');

  function closeMenu() {
    menu.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
  }

  // The menu is position:fixed (so it escapes the activity panel's overflow
  // clip); anchor its top-right corner to the button on each open.
  function openMenu() {
    const r = btn.getBoundingClientRect();
    menu.style.top = (r.bottom + 6) + 'px';
    menu.style.right = (window.innerWidth - r.right) + 'px';
    menu.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
  }

  btn.addEventListener('click', e => {
    e.stopPropagation();
    if (menu.hidden) openMenu();
    else closeMenu();
  });

  // Keep the menu open while interacting with it; close on any outside click.
  menu.addEventListener('click', e => e.stopPropagation());
  document.addEventListener('click', e => {
    if (!menu.hidden && !wrap.contains(e.target)) closeMenu();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !menu.hidden) closeMenu();
  });

  function refreshFilterBadge() {
    const n = activeLogFilterCount();
    btn.classList.toggle('active', n > 0);
    count.hidden = n === 0;
    count.textContent = String(n);
  }

  // Tri-state segmented controls (outcome, cached): exactly one value active.
  menu.querySelectorAll('.log-filter-seg').forEach(seg => {
    const id = seg.dataset.filter;
    seg.addEventListener('click', e => {
      const segBtn = e.target.closest('.lfs-btn');
      if (!segBtn) return;
      _logFilterState[id] = segBtn.dataset.val;
      seg.querySelectorAll('.lfs-btn').forEach(b =>
        b.classList.toggle('active', b === segBtn));
      refreshFilterBadge();
      applyLogFilters();
    });
  });

  // Multi-select chip sets (type): each chip toggles its value in the Set.
  menu.querySelectorAll('.log-filter-chips').forEach(chips => {
    const id = chips.dataset.filter;
    chips.addEventListener('click', e => {
      const chip = e.target.closest('.lfc-btn');
      if (!chip) return;
      const set = _logFilterState[id];
      const val = chip.dataset.val;
      if (set.has(val)) set.delete(val); else set.add(val);
      chip.classList.toggle('active', set.has(val));
      refreshFilterBadge();
      applyLogFilters();
    });
  });
})();

// ── "View full output" modal ────────────────────────────────────────────
const logDetailModal      = document.getElementById('log-detail-modal');
const logDetailModalTitle = document.getElementById('log-detail-modal-title');
const logDetailModalNote  = document.getElementById('log-detail-modal-note');
const logDetailModalBody  = document.getElementById('log-detail-modal-body');
const logDetailModalCopy  = document.getElementById('log-detail-modal-copy');
const logDetailModalClose = document.getElementById('log-detail-modal-close');

let _logDetailText = '';

function openLogDetailModal(step) {
  const key = String(step);
  const d = (_lastData && _lastData[key]) || {};
  const type = (d['type'] || 'other').toLowerCase();
  const meta = LOG_TYPE_META[type] || { label: type.toUpperCase() };
  const cached = _logBodyText.get(key);

  _logDetailText = cached || '';
  logDetailModalTitle.textContent = `${meta.label} · #${step}`;
  logDetailModalBody.textContent = cached != null ? cached : 'Loading…';
  logDetailModalBody.scrollTop = 0;
  if (logTruncated(type, d)) {
    logDetailModalNote.hidden = false;
    logDetailModalNote.textContent = 'Output exceeded the logging limit — showing as much as was captured.';
  } else {
    logDetailModalNote.hidden = true;
  }
  logDetailModalCopy.textContent = 'Copy';
  logDetailModal.hidden = false;

  // The body is usually already cached (the button only renders once the
  // expanded panel has it), but fetch defensively so the modal works even when
  // opened before the body arrived.
  if (cached == null) {
    fetchLogBody(key).then(text => {
      if (logDetailModal.hidden) return;  // closed before it arrived
      _logDetailText = text;
      logDetailModalBody.textContent = text;
    });
  }
}

document.getElementById('log-list').addEventListener('click', e => {
  const btn = e.target.closest('.log-view-full-btn');
  if (btn) openLogDetailModal(btn.dataset.step);
});

logDetailModalClose.addEventListener('click', () => { logDetailModal.hidden = true; });
logDetailModal.addEventListener('click', e => { if (e.target === logDetailModal) logDetailModal.hidden = true; });

logDetailModalCopy.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(_logDetailText);
  } catch (_) {
    const ta = document.createElement('textarea');
    ta.value = _logDetailText;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  logDetailModalCopy.textContent = 'Copied!';
  setTimeout(() => { logDetailModalCopy.textContent = 'Copy'; }, 1500);
});
