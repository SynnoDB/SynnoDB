'use strict';

// Activity log panel: one collapsible <details> per step, appended in order.
// We diff against existing entries and only append the new tail; if any
// existing entry no longer corresponds to a known step (e.g. after a source
// switch), the whole list is rebuilt.

const LOG_TYPE_META = {
  llm:        { label:'LLM',        cls:'lt-llm'      },
  apply_patch:{ label:'Patch',      cls:'lt-patch'     },
  write_file: { label:'Write',      cls:'lt-write'     },
  read_file:  { label:'Read',       cls:'lt-read'      },
  shell:      { label:'Shell',      cls:'lt-shell'     },
  compile:    { label:'Compile',    cls:'lt-compile'   },
  validate:   { label:'Validate',   cls:'lt-validate'  },
  data_inspect:{ label:'Inspect',   cls:'lt-datainspect'},
  compaction: { label:'Compaction', cls:'lt-compaction'},
};

// Activity-log filters. Each is tri-state, keyed by the data-filter attribute on
// its segmented control: 'off' (inactive - the entry always passes), or one of
// the filter's own values. `match(d, state)` decides whether a step's raw data
// record is shown for the given non-off state. An entry is displayed only when
// every filter passes.
const LOG_FILTERS = {
  cached: {
    // 'yes' → only entries served from cache; 'no' → only entries that were not.
    // Each cache-capable step type reports its own cache signal; a step is
    // "from cache" if any of them says so.
    match(d, state) {
      const fromCache = d['answered_from_cache'] === true            // llm
        || d['data_inspect/cached'] === true                        // data_inspect
        || d['shell/cached'] === true                               // shell
        || d['validation/replayed_from_cache'] === true             // validate
        || d['compile/cached'] === true;                            // compile
      return state === 'yes' ? fromCache : !fromCache;
    },
  },
};
const _logFilterState = { cached: 'off' };

function logEntryPasses(d) {
  for (const id in LOG_FILTERS) {
    const state = _logFilterState[id];
    if (state && state !== 'off' && !LOG_FILTERS[id].match(d, state)) return false;
  }
  return true;
}

function activeLogFilterCount() {
  let n = 0;
  for (const id in _logFilterState) if (_logFilterState[id] !== 'off') n++;
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
  if (type === 'apply_patch' || type === 'write_file') return !!d['apply_patch/truncated'];
  if (type === 'read_file') return !!d['read_file/truncated'];
  if (type === 'shell') return !!d['shell/truncated'];
  if (type === 'data_inspect') return !!d['data_inspect/truncated'];
  return false;
}

function logDesc(type, d) {
  if (type === 'llm') {
    const parts = [d['current_prompt_descriptor'], d['agent_name']].filter(Boolean);
    return parts.join(' · ') || 'LLM call';
  }
  if (type === 'apply_patch' || type === 'write_file') {
    const files   = parseJsonField(d['apply_patch/files']);
    const added   = d['apply_patch/added_loc_count'];
    const deleted = d['apply_patch/deleted_loc_count'];
    const failed  = parseJsonField(d['apply_patch/failed']);
    const hasFailed = failed && failed.length;
    const failedStr = hasFailed ? ' ⚠ ' + failed.length + ' failed' : '';
    const delta = (!hasFailed && (added != null || deleted != null))
      ? ' (+' + (added||0) + '/-' + (deleted||0) + ')' : '';
    if (files && files.length) {
      const names = files.map(f => f.split('/').pop());
      const list = names.slice(0,3).join(', ') + (names.length > 3 ? ' +' + (names.length-3) : '');
      return list + delta + failedStr;
    }
    return 'code change' + delta + failedStr;
  }
  if (type === 'read_file') {
    const path = d['read_file/path'];
    return path ? path.split('/').pop() : 'read file';
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
    if (d['validation/compile_error']) return 'compile error';
    const queries = parseJsonField(d['validation/query_ids_executed']);
    const trace = d['validation/trace_mode'];
    const qStr = queries && queries.length ? queries.join(', ') : null;
    const tStr = trace ? 'trace' : (trace === false ? 'no trace' : null);
    const c = d['validation/correct'];
    const result = c === true ? 'correct' : c === false ? 'incorrect' : 'ran';
    return [result, qStr, tStr].filter(Boolean).join(' · ');
  }
  return d['agent_name'] || type;
}

function logBody(type, d) {
  if (type === 'llm') {
    const out = d['llm/output_text'];
    return (out && out.trim()) ? out : '(no text output)';
  }
  if (type === 'apply_patch' || type === 'write_file') {
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
  if (type === 'read_file') {
    const path = d['read_file/path'];
    const out  = d['read_file/output'];
    const parts = [];
    if (path) parts.push('$ read_file ' + path);
    if (out && out.trim()) parts.push(out);
    return parts.join('\n\n') || '(no output)';
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

function updateLog(steps, data) {
  const el = document.getElementById('log-list');
  if (!steps.length) {
    el.innerHTML = '<div class="log-empty">No activity yet…</div>';
    return;
  }

  const empty = el.querySelector('.log-empty');
  if (empty) empty.remove();

  // If the source changed and the existing list contains steps not in the new
  // payload, reset and rebuild from scratch.
  const stepsSet = new Set(steps.map(String));
  const existingEntries = [...el.querySelectorAll('details.log-entry')];
  if (existingEntries.some(d => !stepsSet.has(d.dataset.step))) {
    el.innerHTML = '';
  }

  const existingSteps = new Set(
    [...el.querySelectorAll('details.log-entry')].map(d => d.dataset.step)
  );
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;

  const newSteps = steps.filter(s => !existingSteps.has(String(s)));
  if (!newSteps.length) return;

  const frag = document.createDocumentFragment();
  for (const s of newSteps) {
    const d = data[s] || {};
    const idx = steps.indexOf(s);
    const type = (d['type'] || 'other').toLowerCase();
    const meta = LOG_TYPE_META[type] || { label: type.toUpperCase(), cls:'lt-other' };
    const desc = logDesc(type, d);
    const body = logBody(type, d);
    const expandedMeta = logExpandedMeta(type, d, steps, data, idx);
    _logBodyText.set(String(s), body);
    const viewFullBtn = body.length > LOG_DETAIL_THRESHOLD
      ? `<button class="log-view-full-btn" type="button" data-step="${s}">View full output</button>`
      : '';
    const details = document.createElement('details');
    details.className = 'log-entry';
    details.dataset.step = s;
    details.hidden = !logEntryPasses(d);
    details.innerHTML = `<summary>
        <span class="log-type ${meta.cls}">${esc(meta.label)}</span>
        <span class="log-desc">${esc(desc)}</span>
        <span class="log-turn">#${s}</span>
        <span class="log-chevron">&#9654;</span>
      </summary>
      <div class="log-body"><div class="log-expanded-meta">${esc(expandedMeta)}</div>${viewFullBtn}<pre>${esc(body)}</pre></div>`;
    frag.appendChild(details);
  }
  el.appendChild(frag);
  updateLogFilterEmpty();

  if (atBottom) el.scrollTop = el.scrollHeight;
}

// ── Filter menu ──────────────────────────────────────────────────────────
// Re-evaluate every rendered entry against the active filters and toggle its
// visibility. Called when the filter selection changes (new entries apply the
// predicate at render time in updateLog).
function applyLogFilters() {
  const el = document.getElementById('log-list');
  for (const entry of el.querySelectorAll('details.log-entry')) {
    const d = (_lastData && _lastData[entry.dataset.step]) || {};
    entry.hidden = !logEntryPasses(d);
  }
  updateLogFilterEmpty();
}

// Show a placeholder when active filters hide every entry, so the panel never
// looks mysteriously blank.
function updateLogFilterEmpty() {
  const el = document.getElementById('log-list');
  const entries = el.querySelectorAll('details.log-entry');
  const anyVisible = [...entries].some(e => !e.hidden);
  let placeholder = el.querySelector('.log-filter-empty');
  if (entries.length && !anyVisible && activeLogFilterCount()) {
    if (!placeholder) {
      placeholder = document.createElement('div');
      placeholder.className = 'log-empty log-filter-empty';
      placeholder.textContent = 'No activity matches the active filter.';
      el.appendChild(placeholder);
    }
  } else if (placeholder) {
    placeholder.remove();
  }
}

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

  menu.querySelectorAll('.log-filter-seg').forEach(seg => {
    const id = seg.dataset.filter;
    seg.addEventListener('click', e => {
      const segBtn = e.target.closest('.lfs-btn');
      if (!segBtn) return;
      _logFilterState[id] = segBtn.dataset.val;
      seg.querySelectorAll('.lfs-btn').forEach(b =>
        b.classList.toggle('active', b === segBtn));

      const n = activeLogFilterCount();
      btn.classList.toggle('active', n > 0);
      count.hidden = n === 0;
      count.textContent = String(n);

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
  const d = (_lastData && _lastData[step]) || {};
  const type = (d['type'] || 'other').toLowerCase();
  const meta = LOG_TYPE_META[type] || { label: type.toUpperCase() };
  const text = _logBodyText.get(String(step)) || '';

  _logDetailText = text;
  logDetailModalTitle.textContent = `${meta.label} · #${step}`;
  logDetailModalBody.textContent = text;
  logDetailModalBody.scrollTop = 0;
  if (logTruncated(type, d)) {
    logDetailModalNote.hidden = false;
    logDetailModalNote.textContent = 'Output exceeded the logging limit — showing as much as was captured.';
  } else {
    logDetailModalNote.hidden = true;
  }
  logDetailModalCopy.textContent = 'Copy';
  logDetailModal.hidden = false;
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
