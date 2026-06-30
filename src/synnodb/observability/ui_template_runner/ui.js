let queries = {};
let availableEngines = {};
const _sectionCsvs = {};
let _sectionCounter = 0;

// Source-code inspector state (the "View source code" panel).
let _codeVisible = false;
let _codeFiles = [];
let _codeActiveIdx = 0;

// Profiling modal state. Holds the latest profiled-engine trace:
// { sections: {name: ms}, counts: {name: value}, timeMs: number }.
let _profileVisible = false;
let _profileData = null;

/* Live theme sync with the wrapper site. The initial theme is set in ui.html
   (from ?theme= / this origin's localStorage). The wrapper page can additionally
   keep us in sync when the user toggles its theme by posting
   `window.frames[i].postMessage({ type: 'theme', theme: 'dark' }, '*')`. */
(function () {
  const KEY = 'bespoke-demo-theme';
  function applyTheme(t) {
    if (t !== 'dark' && t !== 'light') t = 'light';
    document.body.setAttribute('data-theme', t);
    try { localStorage.setItem(KEY, t); } catch (e) {}
  }
  window.applyTheme = applyTheme;
  window.addEventListener('message', function (e) {
    const d = e.data;
    if (d && typeof d === 'object' && d.type === 'theme') applyTheme(d.theme);
  });
})();

const PH_COLORS = ['#f59e0b','#f472b6','#60a5fa','#34d399','#c084fc','#fb923c','#22d3ee','#a3e635'];
const SQL_PREVIEW_MIN_HEIGHT = 240;
let _phColors = {};
let _phKeyByUpper = {};
let _sqlTemplate = '';

function _assignColors(keys) {
  _phColors = {};
  _phKeyByUpper = {};
  keys.forEach((k, i) => {
    _phColors[k] = PH_COLORS[i % PH_COLORS.length];
    _phKeyByUpper[k.toUpperCase()] = k;
  });
}

function _hexToRgb(hex) {
  const r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16);
  return r + ',' + g + ',' + b;
}

function _renderSqlHtml(sql) {
  const phKeys = Object.keys(_phColors);
  if (!phKeys.length) return _escHtml(sql);
  const escapedUpper = phKeys.map(k => k.toUpperCase().replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const pattern = new RegExp('\\[(' + escapedUpper.join('|') + ')\\]', 'gi');
  let result = '', lastIndex = 0, match;
  while ((match = pattern.exec(sql)) !== null) {
    result += _escHtml(sql.slice(lastIndex, match.index));
    const key = _phKeyByUpper[match[1].toUpperCase()] || match[1];
    const color = _phColors[key] || '#fff';
    const val = document.getElementById('ph_' + key)?.value ?? match[0];
    result += '<span class="ph-val" data-ph="' + key + '" style="color:' + color +
      ';background:rgba(' + _hexToRgb(color) + ',0.15);border-radius:3px;padding:0 2px;font-weight:600"' +
      ' onmouseenter="_setPhHover(\'' + key + '\',true)" onmouseleave="_setPhHover(\'' + key + '\',false)">' +
      _escHtml(val) + '</span>';
    lastIndex = pattern.lastIndex;
  }
  result += _escHtml(sql.slice(lastIndex));
  return result;
}

function _escHtml(s) {
  const text = s == null ? '' : String(s);
  return text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function _setPhHover(key, active) {
  const color = _phColors[key];
  document.querySelectorAll('.ph-val[data-ph="' + key + '"]').forEach(function(el) {
    el.style.outline = active ? '1px solid ' + color : '';
    el.style.boxShadow = active ? '0 0 5px rgba(' + _hexToRgb(color) + ',0.6)' : '';
  });
  const inp = document.getElementById('ph_' + key);
  if (inp) inp.style.boxShadow = active
    ? '0 0 0 2px rgba(' + _hexToRgb(color) + ',0.55), 0 0 10px rgba(' + _hexToRgb(color) + ',0.25)'
    : '';
}

function updateSqlScrollHint() {
  const pre = document.getElementById('sql-preview');
  const hint = document.getElementById('sql-scroll-hint');
  if (!pre || !hint) return;

  const overflow = pre.scrollHeight > pre.clientHeight + 2;
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 2;
  hint.classList.toggle('visible', overflow && !atBottom);
}

function syncSqlPreviewHeight() {
  const pre = document.getElementById('sql-preview');
  const left = document.getElementById('col-left');
  if (!pre || !left) return;

  const children = Array.from(left.children);
  if (!children.length) return;

  const top = Math.min(...children.map(el => el.getBoundingClientRect().top));
  const bottom = Math.max(...children.map(el => el.getBoundingClientRect().bottom));
  const fieldHeight = Math.ceil(bottom - top);
  if (!fieldHeight) return;

  const height = Math.max(SQL_PREVIEW_MIN_HEIGHT, fieldHeight);
  pre.style.height = height + 'px';
  updateSqlScrollHint();
}

function updateSqlPreview() {
  const pre = document.getElementById('sql-preview');
  if (pre) pre.innerHTML = _sqlTemplate ? _renderSqlHtml(_sqlTemplate) : '';
  updateSqlScrollHint();
}

function renderMetadata(meta) {
  const el = document.getElementById('header-meta');
  if (!el || !meta || !Object.keys(meta).length) return;
  const chips = [];
  if (meta.wandb_run) chips.push({ label: 'wandb-run', value: meta.wandb_run });
  if (meta.turn != null) chips.push({ label: 'turn', value: String(meta.turn) });
  if (meta.git_snapshot_hash) chips.push({ label: 'git hash', value: meta.git_snapshot_hash.slice(0, 8) });
  if (meta.model) chips.push({ label: 'model', value: meta.model.replace(/^.*\//, '') });
  el.innerHTML = chips.map(c =>
    '<span class="meta-chip"><span class="meta-chip-label">' + escHtml(c.label) + '</span>' +
    '<span class="meta-chip-value">' + escHtml(c.value) + '</span></span>'
  ).join('');
}

async function loadQueries() {
  const res = await fetch('/');
  const data = await res.json();
  queries = data.queries;
  renderMetadata(data.code_metadata || {});
  const sel = document.getElementById('qsel');
  sel.innerHTML = '';
  for (const [qid, info] of Object.entries(queries)) {
    const opt = document.createElement('option');
    opt.value = qid;
    opt.textContent = qid + (Object.keys(info.placeholders).length
      ? ' (' + Object.keys(info.placeholders).join(', ') + ')' : '');
    sel.appendChild(opt);
  }
  // Restore query selection from URL
  const urlParams = new URLSearchParams(window.location.search);
  const urlQ = urlParams.get('q');
  if (urlQ) {
    const qidFull = 'q' + urlQ;
    if (queries[qidFull]) sel.value = qidFull;
    else if (queries[urlQ]) sel.value = urlQ;
  } else if (data.benchmark === 'tpch' && queries.q5) {
    sel.value = 'q5';
  }

  sel.onchange = () => { renderPlaceholders(); loadTemplate(); updatePageUrl(); updateCodeBtnLabel(); refreshCodeIfVisible(); };
  renderPlaceholders();

  // Restore placeholder values from URL
  if (urlQ) {
    const qid = sel.value;
    const ph = queries[qid]?.placeholders || {};
    for (const k of Object.keys(ph)) {
      const val = urlParams.get(k);
      if (val != null) { const inp = document.getElementById('ph_' + k); if (inp) inp.value = val; }
    }
    syncSqlPreviewHeight();
    // Restore checkbox states (only when URL has explicit state)
    const umbraCb = document.getElementById('cb-umbra');
    const duckdbCb = document.getElementById('cb-duckdb');
    const clickhouseCb = document.getElementById('cb-clickhouse');
    if (umbraCb) umbraCb.checked = urlParams.has('cmp_umbra');
    if (duckdbCb) duckdbCb.checked = urlParams.has('cmp_duckdb');
    if (clickhouseCb) clickhouseCb.checked = urlParams.has('cmp_clickhouse');
  }

  // Wire checkbox changes to URL update
  ['cb-umbra', 'cb-duckdb', 'cb-clickhouse'].forEach(id => {
    document.getElementById(id)?.addEventListener('change', updatePageUrl);
  });

  loadTemplate();

  // Show checkboxes only for available engines; force-uncheck the rest so a
  // hidden, still-checked box (e.g. the default-checked Umbra one on a deploy
  // without --umbra) can't trigger a doomed run that always 503s.
  availableEngines = data.engines || {};
  let anyEngine = false;
  for (const [name, available] of Object.entries(availableEngines)) {
    const lbl = document.getElementById('lbl-' + name);
    const cb = document.getElementById('cb-' + name);
    if (available) {
      if (lbl) { lbl.style.display = ''; anyEngine = true; }
    } else if (cb) {
      cb.checked = false;
    }
  }
  if (anyEngine) document.getElementById('engine-row').style.display = '';

  updateCodeBtnLabel();
  updatePageUrl();
}

function renderPlaceholders() {
  const qid = document.getElementById('qsel').value;
  const ph = queries[qid]?.placeholders || {};
  _assignColors(Object.keys(ph));
  const div = document.getElementById('placeholders');
  div.innerHTML = '';
  for (const [k, v] of Object.entries(ph)) {
    const color = _phColors[k];
    const lbl = document.createElement('label');
    lbl.textContent = k;
    lbl.setAttribute('for', 'ph_' + k);
    lbl.style.color = color;
    const inp = document.createElement('input');
    inp.id = 'ph_' + k;
    inp.name = k;
    inp.value = v;
    inp.style.borderColor = color;
    inp.addEventListener('focus',      () => _setPhHover(k, true));
    inp.addEventListener('blur',       () => _setPhHover(k, false));
    inp.addEventListener('mouseenter', () => _setPhHover(k, true));
    inp.addEventListener('mouseleave', () => _setPhHover(k, false));
    inp.addEventListener('input', () => { updateSqlPreview(); updatePageUrl(); });
    div.appendChild(lbl);
    div.appendChild(inp);
  }
  requestAnimationFrame(syncSqlPreviewHeight);
}

async function loadTemplate() {
  const qid = document.getElementById('qsel').value;
  const rawId = qid.startsWith('q') ? qid.slice(1) : qid;
  const res = await fetch('/sql/' + rawId);
  const data = await res.json();
  _sqlTemplate = data.template;
  updateSqlPreview();
}

// RFC-4180-ish CSV parser: correctly handles quoted fields containing commas,
// escaped quotes (""), and embedded newlines — none of which the old
// line-split + every-other-match regex approach handled.
function parseCsv(text) {
  const rows = [];
  let row = [], field = '', inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else { inQuotes = false; }
      } else { field += c; }
      continue;
    }
    if (c === '"') { inQuotes = true; }
    else if (c === ',') { row.push(field); field = ''; }
    else if (c === '\r') { /* ignore CR (CRLF line endings) */ }
    else if (c === '\n') { row.push(field); rows.push(row); row = []; field = ''; }
    else { field += c; }
  }
  if (field !== '' || row.length) { row.push(field); rows.push(row); }
  return rows;
}

function parseCsvRows(csv) {
  return parseCsv((csv || '').replace(/[\r\n]+$/, ''));
}

function csvToTable(csv, limit) {
  const rows = parseCsvRows(csv);
  if (!rows.length) return '<em>empty result</em>';
  const displayRows = limit != null ? [rows[0], ...rows.slice(1, limit + 1)] : rows;
  const table = document.createElement('table');
  displayRows.forEach((cells, i) => {
    const tr = table.insertRow();
    cells.forEach(c => {
      const cell = i === 0 ? document.createElement('th') : document.createElement('td');
      cell.textContent = c;
      tr.appendChild(cell);
    });
  });
  return table.outerHTML;
}

function expandSection(sid, limit) {
  const csv = _sectionCsvs[sid];
  const container = document.getElementById('tbl-' + sid);
  const totalDataRows = Math.max(0, parseCsvRows(csv).length - 1);
  const shown = Math.min(totalDataRows, limit);
  const canExpandMore = limit === 100 && totalDataRows > 100;
  container.innerHTML =
    '<div style="max-height:400px;overflow-y:auto;overflow-x:auto">' + csvToTable(csv, limit) + '</div>' +
    (canExpandMore
      ? '<button class="secondary" style="margin-top:.5rem;font-size:.76rem;padding:.28rem .75rem" onclick="expandSection(\'' + sid + '\', Infinity)">Show all ' + totalDataRows + ' rows</button>'
      : '<span style="font-size:.74rem;color:var(--text-muted);margin-top:.35rem;display:block">Showing ' + shown + ' of ' + totalDataRows + ' rows</span>');
}

function updatePageUrl() {
  const qid = document.getElementById('qsel').value;
  const rawId = qid.startsWith('q') ? qid.slice(1) : qid;
  const ph = queries[qid]?.placeholders || {};
  const params = new URLSearchParams();
  params.set('q', rawId);
  for (const k of Object.keys(ph)) {
    params.set(k, document.getElementById('ph_' + k)?.value ?? '');
  }
  if (document.getElementById('cb-umbra')?.checked) params.set('cmp_umbra', '1');
  if (document.getElementById('cb-duckdb')?.checked) params.set('cmp_duckdb', '1');
  if (document.getElementById('cb-clickhouse')?.checked) params.set('cmp_clickhouse', '1');
  history.replaceState(null, '', window.location.pathname + '?' + params);
}

function buildSqlUrl() {
  const qid = document.getElementById('qsel').value;
  const rawId = qid.startsWith('q') ? qid.slice(1) : qid;
  const ph = queries[qid]?.placeholders || {};
  const params = new URLSearchParams();
  for (const k of Object.keys(ph)) {
    params.set(k, document.getElementById('ph_' + k).value);
  }
  return '/sql/' + rawId + (params.toString() ? '?' + params : '');
}

// ── Source-code inspector ────────────────────────────────────────────────
// Shows the generated C++ engine source (queryX.cpp / queryX.hpp) for the
// currently selected query in a modal, served by the backend's /code/<id>
// endpoint.

function _selectedRawId() {
  const qid = document.getElementById('qsel').value || '';
  return qid.startsWith('q') ? qid.slice(1) : qid;
}

// Keep the button label in sync with the selected query, e.g. "View Q5 sourcecode".
function updateCodeBtnLabel() {
  const btn = document.getElementById('code-btn');
  if (!btn) return;
  const rawId = _selectedRawId();
  btn.textContent = rawId ? 'View Q' + rawId + ' sourcecode' : 'View sourcecode';
}

async function openCodeModal() {
  _codeVisible = true;
  document.getElementById('code-modal').classList.add('open');
  await loadCode();
}

function closeCodeModal() {
  _codeVisible = false;
  document.getElementById('code-modal').classList.remove('open');
}

function onCodeModalBackdrop(event) {
  // Close only when the click lands on the overlay itself, not the dialog.
  if (event.target.id === 'code-modal') closeCodeModal();
}

function refreshCodeIfVisible() {
  if (_codeVisible) loadCode();
}

async function loadCode() {
  const rawId = _selectedRawId();
  const body = document.getElementById('code-modal-body');
  const title = document.getElementById('code-modal-title');
  if (title) title.textContent = 'Q' + rawId + ' source code';
  body.innerHTML = '<div class="code-status">Loading source…</div>';
  try {
    const res = await fetch('/code/' + rawId);
    const data = await res.json();
    if (!res.ok) {
      body.innerHTML = '<div class="code-status code-error">' +
        escHtml(data.error || ('Error ' + res.status)) + '</div>';
      return;
    }
    _codeFiles = data.files || [];
    _codeActiveIdx = 0;
    renderCodeView();
  } catch (e) {
    body.innerHTML = '<div class="code-status code-error">Fetch error: ' +
      escHtml(String(e)) + '</div>';
  }
}

function selectCodeTab(idx) {
  _codeActiveIdx = idx;
  renderCodeView();
}

function renderCodeView() {
  const body = document.getElementById('code-modal-body');
  if (!_codeFiles.length) {
    body.innerHTML = '<div class="code-status">No source files available.</div>';
    return;
  }
  if (_codeActiveIdx >= _codeFiles.length) _codeActiveIdx = 0;
  const tabs = _codeFiles.map((f, i) =>
    '<button class="code-tab' + (i === _codeActiveIdx ? ' active' : '') +
    '" onclick="selectCodeTab(' + i + ')">' + escHtml(f.name) + '</button>'
  ).join('');
  const active = _codeFiles[_codeActiveIdx];
  body.innerHTML =
    '<div class="code-tabs">' + tabs + '</div>' +
    '<pre class="code-body">' + escHtml(active.content) + '</pre>';
}

// Close the open modal with the Escape key.
window.addEventListener('keydown', function (e) {
  if (e.key !== 'Escape') return;
  if (_codeVisible) closeCodeModal();
  if (_profileVisible) closeProfileModal();
});

// Render accumulated run errors below the Run button (not as result cards).
// Deduped by message: an invalid-input error is raised by the request layer
// before any engine runs, so it is identical for every engine and shown once —
// not attributed to a system. Genuinely engine-specific failures (e.g. a service
// being unreachable) already carry the engine name in their own message text.
function renderRunError(messages) {
  const el = document.getElementById('run-error');
  if (!el) return;
  el.innerHTML = [...messages].map(msg =>
    '<div class="run-error-item">' + escHtml(msg) + '</div>'
  ).join('');
}

function renderEngineSection(label, timeSuffix, csv) {
  const totalDataRows = Math.max(0, parseCsvRows(csv).length - 1);
  const summaryStyle = 'cursor:pointer;color:var(--text-muted);font-size:.78rem;font-family:var(--font)';
  const sid = 's' + (++_sectionCounter);
  _sectionCsvs[sid] = csv;
  const previewRows = Math.min(totalDataRows, 3);
  const hasMore = totalDataRows > 3;
  return '<div class="engine-section">' +
    '<div class="engine-label">' + escHtml(label) + (timeSuffix ? ' — ' + escHtml(timeSuffix) : '') +
      ' (' + totalDataRows + ' rows)</div>' +
    '<div id="tbl-' + sid + '">' +
      csvToTable(csv, 3) +
      (hasMore
        ? '<button class="secondary" style="margin-top:.5rem;font-size:.76rem;padding:.28rem .75rem" onclick="expandSection(\'' + sid + '\', 100)">Show 100 rows</button>' +
          '<span style="font-size:.74rem;color:var(--text-muted);margin-left:.6rem">showing ' + previewRows + ' of ' + totalDataRows + '</span>'
        : '') +
    '</div>' +
    '<br><details><summary style="' + summaryStyle + '">raw CSV</summary><div id="raw">' + escHtml(csv) + '</div></details>' +
    '</div>';
}

function renderBarChart(timings) {
  const entries = Object.entries(timings);
  if (entries.length < 1) return '';
  const maxVal = Math.max(...entries.map(([, v]) => v));
  const chartH = 120, barW = 70, gap = 24;
  const totalW = entries.length * (barW + gap) + gap;
  const colors = { 'SynnoDB(BespokeOLAP)': '#14b8a6', 'DuckDB': '#60a5fa', 'Umbra': '#f59e0b', 'ClickHouse': '#fb7185' };
  const showProfileBtn = profileHasData();
  const infoIcon = '<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="flex:none"><circle cx="12" cy="12" r="10"/><line x1="12" y1="11" x2="12" y2="16"/><circle cx="12" cy="8" r="0.6" fill="currentColor" stroke="none"/></svg>';
  let svg = `<svg width="${totalW}" height="${chartH + (showProfileBtn ? 88 : 52)}" style="overflow:visible;display:block">`;
  entries.forEach(([name, val], i) => {
    const barH = Math.max(2, Math.round((val / maxVal) * chartH));
    const x = gap + i * (barW + gap);
    const y = chartH - barH;
    const color = colors[name] || 'var(--text-muted)';
    svg += `<rect x="${x}" y="${y}" width="${barW}" height="${barH}" fill="${color}" rx="4" fill-opacity="0.85"/>`;
    svg += `<text x="${x + barW / 2}" y="${y - 7}" text-anchor="middle" style="fill:var(--text)" font-size="11" font-family="ui-monospace,monospace">${val.toFixed(3)}s</text>`;
    svg += `<text x="${x + barW / 2}" y="${chartH + 20}" text-anchor="middle" style="fill:var(--text-dim)" font-size="11" font-family="system-ui,sans-serif">${escHtml(name)}</text>`;
    // Profiling button, centered below the bespoke bar only.
    if (showProfileBtn && name === 'SynnoDB(BespokeOLAP)') {
      const bw = 130, bx = x + barW / 2 - bw / 2, by = chartH + 32;
      svg += `<foreignObject x="${bx}" y="${by}" width="${bw}" height="36">` +
        '<div xmlns="http://www.w3.org/1999/xhtml" style="display:flex;justify-content:center">' +
          '<button class="secondary" onclick="openProfileModal()" style="font-size:.72rem;padding:.26rem .7rem;display:inline-flex;align-items:center;gap:.35rem;white-space:nowrap">' +
            infoIcon + '<span>Profiling</span>' +
          '</button>' +
        '</div>' +
      '</foreignObject>';
    }
  });
  svg += '</svg>';
  return '<div style="margin-top:1.4rem;padding:1.1rem 1.3rem;background:var(--bg-card);border:1px solid var(--border);border-radius:10px;box-shadow:var(--card-shadow)">' +
    '<div style="font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.09em;color:var(--text-muted);margin-bottom:1rem">Runtime comparison</div>' +
    svg + '</div>';
}

// ── Profiling modal ──────────────────────────────────────────────────────
// Shows the profiled bespoke engine's trace: per-section timings (as a share
// of total runtime) plus cardinality / other counters. Data is captured into
// _profileData when the profiled engine completes (see runQuery).

function profileHasData() {
  if (!_profileData) return false;
  const s = _profileData.sections || {};
  const c = _profileData.counts || {};
  return Object.keys(s).length > 0 || Object.keys(c).length > 0;
}

function openProfileModal() {
  if (!profileHasData()) return;
  _profileVisible = true;
  renderProfileModal();
  document.getElementById('profile-modal').classList.add('open');
}

function closeProfileModal() {
  _profileVisible = false;
  document.getElementById('profile-modal').classList.remove('open');
}

function onProfileModalBackdrop(event) {
  if (event.target.id === 'profile-modal') closeProfileModal();
}

function renderProfileModal() {
  const body = document.getElementById('profile-modal-body');
  if (!body) return;
  const sections = _profileData.sections || {};
  const counts = _profileData.counts || {};
  // Preserve the engine's original emission order (do not re-sort).
  const sEntries = Object.entries(sections)
    .filter(([, ms]) => typeof ms === 'number' && ms >= 0);
  // Denominator for "% of total runtime": the measured run total, but never
  // smaller than the largest section (which is the enclosing *_total scope),
  // so percentages stay within 0–100%.
  const maxSection = sEntries.length ? Math.max(...sEntries.map(([, ms]) => ms)) : 0;
  const total = Math.max(_profileData.timeMs || 0, maxSection) || 1;

  let html = '<div style="font-size:.8rem;color:var(--text-muted);margin-bottom:1rem">' +
    'Total runtime <span style="font-family:var(--font-mono);color:var(--text)">' +
    total.toFixed(3) + ' ms</span> ' +
    '<span style="font-size:.72rem">(profiled run — includes tracing overhead)</span></div>';

  if (sEntries.length) {
    const rows = sEntries.map(([name, ms]) => {
      const pct = (ms / total) * 100;
      return '<div style="display:flex;align-items:center;gap:.7rem;margin:.3rem 0;font-size:.8rem">' +
        '<div style="flex:0 0 34%;font-family:var(--font-mono);color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + escHtml(name) + '">' + escHtml(name) + '</div>' +
        '<div style="flex:1;background:var(--bg);border-radius:4px;overflow:hidden">' +
          '<div style="width:' + Math.max(1, pct).toFixed(1) + '%;height:15px;background:#14b8a6;border-radius:4px;opacity:.85"></div>' +
        '</div>' +
        '<div style="flex:0 0 4.2rem;text-align:right;font-family:var(--font-mono);color:var(--text)">' + pct.toFixed(1) + '%</div>' +
        '<div style="flex:0 0 6rem;text-align:right;font-family:var(--font-mono);color:var(--text-muted)">' + ms.toFixed(3) + ' ms</div>' +
      '</div>';
    }).join('');
    html += '<div style="font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.09em;color:var(--text-muted);margin:.4rem 0 .7rem">Time by section</div>' + rows;
  }

  // Preserve the engine's original emission order (do not re-sort).
  const cEntries = Object.entries(counts);
  if (cEntries.length) {
    const rows = cEntries.map(([name, val]) =>
      '<div style="display:flex;align-items:center;justify-content:space-between;gap:.7rem;margin:.25rem 0;font-size:.8rem;border-bottom:1px solid var(--border);padding-bottom:.25rem">' +
        '<div style="font-family:var(--font-mono);color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + escHtml(name) + '">' + escHtml(name) + '</div>' +
        '<div style="font-family:var(--font-mono);color:var(--text-muted)">' + Number(val).toLocaleString() + '</div>' +
      '</div>'
    ).join('');
    html += '<div style="font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.09em;color:var(--text-muted);margin:1.4rem 0 .7rem">Cardinality &amp; counters</div>' + rows;
  }

  if (!sEntries.length && !cEntries.length) {
    html += '<div style="color:var(--text-muted);font-size:.82rem">No profiling data was emitted for this query.</div>';
  }
  body.innerHTML = html;
}

async function runQuery() {
  // Gate on engine availability, not just the checkbox: a hidden-but-checked
  // box must not trigger a doomed /run_engine call that always 503s.
  const runDuckdb = !!availableEngines.duckdb && document.getElementById('cb-duckdb').checked;
  const runUmbra = !!availableEngines.umbra && document.getElementById('cb-umbra').checked;
  const runClickhouse = !!availableEngines.clickhouse && document.getElementById('cb-clickhouse').checked;
  // The profiled bespoke engine (compiled with -DTRACE) runs automatically
  // alongside the clean one whenever its service is available.
  const runProfiled = !!availableEngines.bespoke_profiled;
  document.getElementById('run-btn').disabled = true;
  document.getElementById('output').innerHTML = '';
  document.getElementById('chart').innerHTML = '';
  document.getElementById('run-error').innerHTML = '';

  const qid = document.getElementById('qsel').value;
  const rawId = qid.startsWith('q') ? qid.slice(1) : qid;
  const ph = queries[qid]?.placeholders || {};
  const params = new URLSearchParams();
  for (const k of Object.keys(ph)) params.set(k, document.getElementById('ph_' + k).value);
  const runId = (window.crypto && window.crypto.randomUUID) ? window.crypto.randomUUID() : String(Date.now()) + '-' + Math.random().toString(16).slice(2);
  params.set('run_id', runId);
  const paramStr = params.toString() ? '?' + params : '';
  const url = '/run/' + rawId + paramStr;

  const timings = {};
  _profileData = null;
  if (_profileVisible) closeProfileModal();
  let html = '';
  // Errors are shown below the Run button rather than as result cards, deduped
  // by message so a request-level error (identical for every engine) appears
  // once instead of once per engine.
  const runErrors = new Set();

  const setStatus = (msg) => { document.getElementById('status').textContent = msg; };
  const flush = () => { document.getElementById('output').innerHTML = html; };
  const updateChart = () => { document.getElementById('chart').innerHTML = renderBarChart(timings); };
  const showError = (msg) => {
    runErrors.add(msg == null ? 'Unknown error' : String(msg));
    renderRunError(runErrors);
  };

  // Engines to run, in order. BespokeOLAP always runs; baselines are opt-in.
  // All engines share the same fetch → render path below.
  const engineConfigs = [
    { label: 'SynnoDB(BespokeOLAP)', url: url },
    ...(runProfiled ? [{ label: 'SynnoDB (profiled)', url: '/run_profiled/' + rawId + paramStr, profiled: true }] : []),
    ...(runUmbra ? [{ label: 'Umbra', url: '/run_engine/umbra/' + rawId + paramStr }] : []),
    ...(runDuckdb ? [{ label: 'DuckDB', url: '/run_engine/duckdb/' + rawId + paramStr }] : []),
    ...(runClickhouse ? [{ label: 'ClickHouse', url: '/run_engine/clickhouse/' + rawId + paramStr }] : []),
  ];

  // Progress steps
  const engines = engineConfigs.map(c => c.label);
  const doneSet = new Set();
  const renderProgress = () => {
    const progressEl = document.getElementById('progress');
    if (doneSet.size === engines.length) { progressEl.innerHTML = ''; return; }
    progressEl.innerHTML = '<div style="display:flex;gap:1.2rem;padding:.6rem 0;font-size:.82rem">' +
      engines.map(e => {
        const done = doneSet.has(e);
        const active = !done && doneSet.size === engines.indexOf(e);
        const color = done ? 'var(--accent)' : active ? 'var(--text)' : 'var(--text-muted)';
        const icon = done ? '✓' : active ? '◌' : '○';
        return `<span style="color:${color}">${icon} ${e}</span>`;
      }).join('') +
    '</div>';
  };
  renderProgress();

  try {
    // Fetch assembled SQL (fast metadata call, no progress needed)
    const sqlData = await fetch(buildSqlUrl()).then(r => r.json());
    const assembledSql = sqlData.assembled ?? sqlData.template;
    // On invalid input the /sql preview also 400s (no assembled SQL); skip the
    // empty details box so the result area stays clean — the error shows below
    // the Run button instead.
    html = assembledSql
      ? '<details><summary style="cursor:pointer;color:var(--text-muted);font-size:.78rem">assembled SQL</summary>' +
        '<pre id="raw">' + escHtml(assembledSql) + '</pre></details>'
      : '';
    flush();

    // Run each engine through the same fetch → render path so results and
    // errors look identical regardless of which engine produced them.
    for (const cfg of engineConfigs) {
      setStatus('Running ' + cfg.label + '…');
      const resp = await fetch(cfg.url);
      const data = await resp.json();
      if (!resp.ok) {
        showError(data.error ?? JSON.stringify(data));
        setStatus(cfg.label + ' error ' + resp.status);
      } else {
        const elapsed = data.time_ms / 1000;
        if (cfg.profiled) {
          // The profiled run carries trace overhead, so it must NOT compete in
          // the benchmark bar chart and its (identical) result table is not
          // re-shown. Its trace feeds the "Profiling details" modal instead.
          const p = data.profile || {};
          _profileData = {
            sections: p.sections || {},
            counts: p.counts || {},
            timeMs: data.time_ms,
          };
          if (_profileVisible) renderProfileModal();
        } else {
          timings[cfg.label] = elapsed;
          html += renderEngineSection(cfg.label, elapsed.toFixed(3) + 's', data.csv);
        }
        setStatus(cfg.label + ' done (' + elapsed.toFixed(3) + 's)');
      }
      doneSet.add(cfg.label); renderProgress(); flush(); updateChart();
    }

    setStatus(runErrors.size ? 'Done (with errors).' : 'Done.');
  } catch(e) {
    showError('Fetch error: ' + e);
    setStatus('Fetch error: ' + e);
  }
  document.getElementById('run-btn').disabled = false;
}

function escHtml(s) {
  const text = s == null ? '' : String(s);
  return text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

const _sqlPreviewEl = document.getElementById('sql-preview');
if (_sqlPreviewEl) {
  _sqlPreviewEl.addEventListener('scroll', updateSqlScrollHint);
  window.addEventListener('resize', () => { syncSqlPreviewHeight(); updateSqlScrollHint(); });
}

loadQueries();
