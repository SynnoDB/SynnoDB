let queries = {};
const _sectionCsvs = {};
let _sectionCounter = 0;

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

  sel.onchange = () => { renderPlaceholders(); loadTemplate(); updatePageUrl(); };
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
  updatePageUrl();

  // Show engine checkboxes only for available engines
  const engines = data.engines || {};
  let anyEngine = false;
  for (const [name, available] of Object.entries(engines)) {
    if (available) {
      const lbl = document.getElementById('lbl-' + name);
      if (lbl) { lbl.style.display = ''; anyEngine = true; }
    }
  }
  if (anyEngine) document.getElementById('engine-row').style.display = '';
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

function csvToTable(csv, limit) {
  const lines = csv.trim().split('\n').filter(Boolean);
  if (!lines.length) return '<em>empty result</em>';
  const displayLines = limit != null ? [lines[0], ...lines.slice(1, limit + 1)] : lines;
  const table = document.createElement('table');
  displayLines.forEach((line, i) => {
    const row = table.insertRow();
    const cells = line.match(/("(?:[^"]|"")*"|[^,]*)/g)
                      .filter((_, idx) => idx % 2 === 0)
                      .map(c => c.replace(/^"|"$/g, '').replace(/""/g, '"'));
    cells.forEach(c => {
      const cell = i === 0 ? document.createElement('th') : document.createElement('td');
      cell.textContent = c;
      row.appendChild(cell);
    });
  });
  return table.outerHTML;
}

function expandSection(sid, limit) {
  const csv = _sectionCsvs[sid];
  const container = document.getElementById('tbl-' + sid);
  const dataLines = csv.trim().split('\n').filter(Boolean);
  const totalDataRows = dataLines.length - 1;
  const shown = Math.min(totalDataRows, limit);
  const canExpandMore = limit === 100 && totalDataRows > 100;
  container.innerHTML =
    '<div style="max-height:400px;overflow-y:auto;overflow-x:auto">' + csvToTable(csv, limit) + '</div>' +
    (canExpandMore
      ? '<button class="secondary" style="margin-top:.5rem;font-size:.76rem;padding:.28rem .75rem" onclick="expandSection(\'' + sid + '\', Infinity)">Show all ' + totalDataRows + ' rows</button>'
      : '<span style="font-size:.74rem;color:#9a856d;margin-top:.35rem;display:block">Showing ' + shown + ' of ' + totalDataRows + ' rows</span>');
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

function buildUrl(absolute) {
  const qid = document.getElementById('qsel').value;
  const rawId = qid.startsWith('q') ? qid.slice(1) : qid;
  const ph = queries[qid]?.placeholders || {};
  const params = new URLSearchParams();
  for (const k of Object.keys(ph)) {
    params.set(k, document.getElementById('ph_' + k).value);
  }
  const rel = '/run/' + rawId + (params.toString() ? '?' + params : '');
  return absolute ? (window.location.origin + rel) : rel;
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

async function showQuery() {
  const res = await fetch(buildSqlUrl());
  const data = await res.json();
  const sql = data.assembled ?? data.template;
  const absUrl = window.location.origin + buildUrl(false);
  const curlCmd = `curl -s "${absUrl}" -o result.csv && cat result.csv`;
  document.getElementById('status').textContent = '';
  document.getElementById('output').innerHTML =
    '<pre id="raw">' + escHtml(sql) + '</pre>' +
    '<div style="margin-top:.8rem;background:#fffaf3;border:1px solid #deceb4;border-radius:8px;padding:.9rem">' +
      '<div style="font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.09em;color:#9a856d;margin-bottom:.4rem">API endpoint</div>' +
      '<div style="color:#1b8d6e;font-size:.83rem;word-break:break-all;font-family:var(--font-mono)">' + escHtml(absUrl) + '</div>' +
      '<div style="font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.09em;color:#9a856d;margin-top:.7rem;margin-bottom:.4rem">curl</div>' +
      '<div style="color:#1b8d6e;font-size:.83rem;word-break:break-all;font-family:var(--font-mono)">' + escHtml(curlCmd) + '</div>' +
    '</div>';
}

function renderEngineSection(label, timeSuffix, csv) {
  const lines = csv.trim().split('\n').filter(Boolean);
  const totalDataRows = lines.length - 1;
  const summaryStyle = 'cursor:pointer;color:#9a856d;font-size:.78rem;font-family:var(--font)';
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
          '<span style="font-size:.74rem;color:#9a856d;margin-left:.6rem">showing ' + previewRows + ' of ' + totalDataRows + '</span>'
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
  const colors = { 'BespokeOLAP': '#14b8a6', 'DuckDB': '#60a5fa', 'Umbra': '#f59e0b', 'ClickHouse': '#fb7185' };
  let svg = `<svg width="${totalW}" height="${chartH + 52}" style="overflow:visible;display:block">`;
  entries.forEach(([name, val], i) => {
    const barH = Math.max(2, Math.round((val / maxVal) * chartH));
    const x = gap + i * (barW + gap);
    const y = chartH - barH;
    const color = colors[name] || '#6f5d49';
    svg += `<rect x="${x}" y="${y}" width="${barW}" height="${barH}" fill="${color}" rx="4" fill-opacity="0.85"/>`;
    svg += `<text x="${x + barW / 2}" y="${y - 7}" text-anchor="middle" fill="#31261d" font-size="11" font-family="ui-monospace,monospace">${val.toFixed(3)}s</text>`;
    svg += `<text x="${x + barW / 2}" y="${chartH + 20}" text-anchor="middle" fill="#6f5d49" font-size="11" font-family="system-ui,sans-serif">${escHtml(name)}</text>`;
  });
  svg += '</svg>';
  return '<div style="margin-top:1.4rem;padding:1.1rem 1.3rem;background:#fffaf3;border:1px solid #deceb4;border-radius:10px">' +
    '<div style="font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.09em;color:#9a856d;margin-bottom:1rem">Runtime comparison</div>' + svg + '</div>';
}

async function runQuery() {
  const runDuckdb = document.getElementById('cb-duckdb').checked;
  const runUmbra = document.getElementById('cb-umbra').checked;
  const runClickhouse = document.getElementById('cb-clickhouse').checked;
  document.getElementById('run-btn').disabled = true;
  const showBtn = document.getElementById('show-btn');
  if (showBtn) showBtn.disabled = true;
  document.getElementById('output').innerHTML = '';
  document.getElementById('chart').innerHTML = '';

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
  let html = '';

  const setStatus = (msg) => { document.getElementById('status').textContent = msg; };
  const flush = () => { document.getElementById('output').innerHTML = html; };
  const updateChart = () => { document.getElementById('chart').innerHTML = renderBarChart(timings); };

  // Progress steps
  const engines = ['BespokeOLAP', ...(runUmbra ? ['Umbra'] : []), ...(runDuckdb ? ['DuckDB'] : []), ...(runClickhouse ? ['ClickHouse'] : [])];
  const doneSet = new Set();
  const renderProgress = () => {
    const progressEl = document.getElementById('progress');
    if (doneSet.size === engines.length) { progressEl.innerHTML = ''; return; }
    progressEl.innerHTML = '<div style="display:flex;gap:1.2rem;padding:.6rem 0;font-size:.82rem">' +
      engines.map(e => {
        const done = doneSet.has(e);
        const active = !done && doneSet.size === engines.indexOf(e);
        const color = done ? '#1b8d6e' : active ? '#31261d' : '#9a856d';
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
    html = '<details><summary style="cursor:pointer;color:#9a856d;font-size:.78rem">assembled SQL</summary>' +
      '<pre id="raw">' + escHtml(assembledSql) + '</pre></details>';
    flush();

    // --- BespokeOLAP ---
    setStatus('Running BespokeOLAP…');
    const bespokeResp = await fetch(url);
    const bespokeData = await bespokeResp.json();
    if (!bespokeResp.ok) {
      setStatus('Error ' + bespokeResp.status);
      html += '<div id="raw">' + escHtml(bespokeData.error ?? JSON.stringify(bespokeData)) + '</div>';
    } else {
      const bespokeElapsed = bespokeData.time_ms / 1000;
      timings['BespokeOLAP'] = bespokeElapsed;
      html += renderEngineSection('BespokeOLAP', bespokeElapsed.toFixed(3) + 's', bespokeData.csv, true);
      setStatus('BespokeOLAP done (' + bespokeElapsed.toFixed(3) + 's)');
    }
    doneSet.add('BespokeOLAP'); renderProgress(); flush(); updateChart();

    // --- Umbra ---
    if (runUmbra) {
      setStatus('Running Umbra…');
      const umbraResp = await fetch('/run_engine/umbra/' + rawId + paramStr);
      const umbraData = await umbraResp.json();
      if (!umbraResp.ok) {
        html += '<div class="engine-section"><div class="engine-label">Umbra</div>' +
          '<div style="color:#f66">Error: ' + escHtml(umbraData.error ?? '') + '</div></div>';
      } else {
        const t = umbraData.time_ms / 1000;
        timings['Umbra'] = t;
        html += renderEngineSection('Umbra', t.toFixed(3) + 's', umbraData.csv, true);
        setStatus('Umbra done (' + t.toFixed(3) + 's)');
      }
      doneSet.add('Umbra'); renderProgress(); flush(); updateChart();
    }

    // --- DuckDB ---
    if (runDuckdb) {
      setStatus('Running DuckDB…');
      const duckdbResp = await fetch('/run_engine/duckdb/' + rawId + paramStr);
      const duckdbData = await duckdbResp.json();
      if (!duckdbResp.ok) {
        html += '<div class="engine-section"><div class="engine-label">DuckDB</div>' +
          '<div style="color:#f66">Error: ' + escHtml(duckdbData.error ?? '') + '</div></div>';
      } else {
        const t = duckdbData.time_ms / 1000;
        timings['DuckDB'] = t;
        html += renderEngineSection('DuckDB', t.toFixed(3) + 's', duckdbData.csv, true);
        setStatus('DuckDB done (' + t.toFixed(3) + 's)');
      }
      doneSet.add('DuckDB'); renderProgress(); flush(); updateChart();
    }

    // --- ClickHouse ---
    if (runClickhouse) {
      setStatus('Running ClickHouse…');
      const chResp = await fetch('/run_engine/clickhouse/' + rawId + paramStr);
      const chData = await chResp.json();
      if (!chResp.ok) {
        html += '<div class="engine-section"><div class="engine-label">ClickHouse</div>' +
          '<div style="color:#f66">Error: ' + escHtml(chData.error ?? '') + '</div></div>';
      } else {
        const t = chData.time_ms / 1000;
        timings['ClickHouse'] = t;
        html += renderEngineSection('ClickHouse', t.toFixed(3) + 's', chData.csv, true);
        setStatus('ClickHouse done (' + t.toFixed(3) + 's)');
      }
      doneSet.add('ClickHouse'); renderProgress(); flush(); updateChart();
    }

    setStatus('Done.');
  } catch(e) {
    setStatus('Fetch error: ' + e);
  }
  document.getElementById('run-btn').disabled = false;
  if (showBtn) showBtn.disabled = false;
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
