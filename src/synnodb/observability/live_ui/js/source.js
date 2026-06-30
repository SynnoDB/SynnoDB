'use strict';

// Source selector + endpoint auto-discovery for the standalone dashboard.
// The header input accepts a W&B run id, a /path/to.duckdb, or http://host:8765.
// First-load also honours ?api_url=, ?db=, ?wandb_run_id= query params.

let _sourceType = null;            // 'db' | 'wandb' | 'remote' | null (live pipeline)
let _sourceInputTouched   = false;
let _applyingInitialSource = false;
let _initialSourceApplied  = false;

// ── URL <-> body translation ─────────────────────────────────────────────
function firstQueryValue(params, names) {
  for (const name of names) {
    const value = params.get(name);
    if (value != null && value.trim()) return value.trim();
  }
  return '';
}

function sourceBodyFromValue(val) {
  const isRemoteApi = /^https?:\/\//i.test(val);
  const isPath = val.startsWith('/') || val.startsWith('~') || val.startsWith('.') || val.endsWith('.duckdb');
  return isRemoteApi ? {api_url: val} : (isPath ? {db_path: val} : {wandb_run_id: val});
}

function initialSourceFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const apiUrl = firstQueryValue(params, ['api_url', 'api-url', 'api']);
  if (apiUrl) return {body: {api_url: apiUrl}, display: apiUrl};

  const dbPath = firstQueryValue(params, ['db', 'database', 'database_file', 'db_path', 'db-path']);
  if (dbPath) return {body: {db_path: dbPath}, display: dbPath};

  const wandbRunId = firstQueryValue(params, ['wandb_run_id', 'wandb-run-id', 'wandb_id', 'wandb-id', 'wandb']);
  if (wandbRunId) {
    const body = {wandb_run_id: wandbRunId};
    const wandbEntity  = firstQueryValue(params, ['wandb_entity', 'wandb-entity', 'entity']);
    const wandbProject = firstQueryValue(params, ['wandb_project', 'wandb-project', 'project']);
    if (wandbEntity)  body.wandb_entity  = wandbEntity;
    if (wandbProject) body.wandb_project = wandbProject;
    return {body, display: wandbRunId};
  }

  return null;
}

function updateSourceUrl(body) {
  const url = new URL(window.location.href);
  for (const key of [
    'api_url', 'api-url', 'api',
    'db', 'database', 'database_file', 'db_path', 'db-path',
    'wandb_run_id', 'wandb-run-id', 'wandb_id', 'wandb-id', 'wandb',
    'wandb_entity', 'wandb-entity', 'entity',
    'wandb_project', 'wandb-project', 'project',
  ]) url.searchParams.delete(key);

  if (body.api_url) {
    url.searchParams.set('api_url', body.api_url);
  } else if (body.db_path) {
    url.searchParams.set('db', body.db_path);
  } else if (body.wandb_run_id) {
    url.searchParams.set('wandb_run_id', body.wandb_run_id);
    if (body.wandb_entity)  url.searchParams.set('wandb_entity',  body.wandb_entity);
    if (body.wandb_project) url.searchParams.set('wandb_project', body.wandb_project);
  }

  window.history.replaceState({}, '', url);
}

function updateSourceUI(meta) {
  if (!meta) return;
  _sourceType = meta._source_type || null;

  const dot = document.querySelector('.dot');
  if (dot) dot.classList.toggle('cached', _sourceType === 'wandb');

  const input = document.getElementById('source-input');
  if (input && !_sourceInputTouched && meta._source_ref) {
    input.value = meta._source_ref;
    _sourceInputTouched = false; // keep updating until the user actually types
  }
}

function _tsTxt(meta) {
  if (meta?._source_type === 'wandb') return 'Cached — ↻ to reload';
  return 'Updated ' + new Date().toLocaleTimeString();
}

// ── Switch / reload ──────────────────────────────────────────────────────
const sourceInput     = document.getElementById('source-input');
const sourceSwitchBtn = document.getElementById('source-switch-btn');
const reloadBtn       = document.getElementById('reload-btn');

sourceInput.addEventListener('input', () => { _sourceInputTouched = true; });
sourceInput.addEventListener('blur',  () => {
  if (!sourceInput.value.trim()) _sourceInputTouched = false;
});

async function switchSource() {
  const val = sourceInput.value.trim();
  if (!val) return;
  await switchSourceBody(sourceBodyFromValue(val));
}

function _sourceRefFromBody(body) {
  return body.api_url || body.db_path || body.wandb_run_id || null;
}

// Wipe every panel back to its empty state. Called on source switch so that if
// the new run is missing data for a panel (e.g. the query bar chart), the old
// run's values don't linger on screen.
function resetDashboardData() {
  _lastSteps = [];
  _lastData  = {};
  timeTravelStep = null;
  hoveredDesc    = null;

  updateCards([], {});
  updatePrompts([], {});
  updateCorrectness([], {});
  updateScaleFactorButtons([], {});
  updateChart([], {});
  updateQueryChart([], {});
  updateLog([], {});

  const ind = document.getElementById('timetravel-indicator');
  const btn = document.getElementById('timetravel-live-btn');
  if (ind) { ind.textContent = ''; ind.hidden = true; }
  if (btn) btn.hidden = true;

  if (typeof distModal !== 'undefined' && distModal && !distModal.hidden) {
    if (distChartMode === 'pie') updatePieChart([], {});
    else updateTimelineDistChart([], {}, distChartMode);
    updateBarChart([], {});
  }
}

async function switchSourceBody(body) {
  // Drop re-entrant calls: the button being disabled means a switch is already
  // in flight. Without this an Enter-key spam (or Enter after a slow switch)
  // fires concurrent /api/switch POSTs whose final backend state depends on
  // request arrival order.
  if (sourceSwitchBtn.disabled) return;
  sourceSwitchBtn.disabled = true;
  // Set the expected source ref *before* the POST so that any /api/stats
  // already in flight for the previous source is recognized as stale and
  // dropped by poll() instead of briefly flashing the old run. Track the
  // previous ref so we can restore it if the switch fails — otherwise polls
  // for the still-active old source would be dropped and the UI would freeze.
  const previousRef = _expectedSourceRef;
  _expectedSourceRef = _sourceRefFromBody(body);
  let switched = false;
  try {
    const r = await fetch('/api/switch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const res = await r.json();
    if (!res.ok) { alert('Switch failed: ' + (res.error || 'unknown error')); return; }
    switched = true;
    updateSourceUrl(body);
    _sourceInputTouched = false;
    resetDashboardData();
    document.getElementById('ts-txt').textContent = 'Switching…';
    await poll();
  } catch(e) {
    alert('Switch error: ' + e.message);
  } finally {
    if (!switched) _expectedSourceRef = previousRef;
    sourceSwitchBtn.disabled = false;
  }
}

async function applyInitialSourceFromUrl() {
  const source = initialSourceFromUrl();
  if (!source) return false;
  _initialSourceApplied = true;
  _applyingInitialSource = true;
  if (sourceInput) sourceInput.value = source.display;
  document.getElementById('ts-txt').textContent = 'Loading source from URL...';
  try {
    await switchSourceBody(source.body);
  } finally {
    _applyingInitialSource = false;
  }
  return true;
}

async function reloadData() {
  reloadBtn.classList.add('spinning');
  try {
    await fetch('/api/reload', {method: 'POST'});
    document.getElementById('ts-txt').textContent = 'Reloading…';
    await poll();
    const reloadTimeEl = document.getElementById('hdr-reload-time');
    if (reloadTimeEl) reloadTimeEl.textContent =
      new Date().toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'});
  } catch(e) {
    document.getElementById('ts-txt').textContent = 'Error: ' + e.message;
  } finally {
    reloadBtn.classList.remove('spinning');
  }
}

sourceSwitchBtn.addEventListener('click', switchSource);
sourceInput.addEventListener('keydown', e => { if (e.key === 'Enter') switchSource(); });
reloadBtn.addEventListener('click', reloadData);

// ── Endpoint auto-discovery (cluster hosts) ──────────────────────────────
// Don't probe ourselves: when the standalone dashboard is served from one of
// the cluster hosts (e.g. opened at http://c03.lab.tuda.systems:8765), that
// host's /api/stats *is* this server. Probing it would list the dashboard as a
// remote source and, if selected, make it proxy itself. Drop any discovery URL
// whose host:port matches the page we're served from.
function _isSelfUrl(url) {
  try { return new URL(url).host === window.location.host; }
  catch { return false; }
}
const _DISC_HOSTS = [
  ...Array.from({length: 9}, (_, i) => `http://c${String(i+1).padStart(2,'0')}.lab.tuda.systems:8765`),
  ...Array.from({length: 6}, (_, i) => `http://fn${String(i+1).padStart(2,'0')}.lab.tuda.systems:8765`),
].filter(url => !_isSelfUrl(url));
const _DISC_GROUPS = [{label: 'Cluster Autodiscovery', urls: _DISC_HOSTS}];
const _discCache   = new Map();
const _sourceDropdown = document.getElementById('source-dropdown');

async function _discProbe(url) {
  if (_discCache.has(url) && _discCache.get(url).status !== 'checking') return;
  _discCache.set(url, {status: 'checking', runName: null, systemName: null});
  try {
    const ctrl = new AbortController();
    const tid = setTimeout(() => ctrl.abort(), 2500);
    let r;
    try { r = await fetch(url + '/api/stats', {signal: ctrl.signal}); }
    finally { clearTimeout(tid); }
    if (r.ok) {
      const json = await r.json();
      const meta = json?.meta || {};
      _discCache.set(url, {status: 'available', runName: meta.run_name || null, systemName: meta.system_name || null});
    } else {
      _discCache.set(url, {status: 'unavailable', runName: null, systemName: null});
    }
  } catch {
    _discCache.set(url, {status: 'unavailable', runName: null, systemName: null});
  }
  _discRenderItem(url);
}

function _discRenderItem(url) {
  if (!_sourceDropdown || _sourceDropdown.hidden) return;
  const item = [..._sourceDropdown.querySelectorAll('[data-disc-url]')].find(el => el.dataset.discUrl === url);
  if (!item) return;
  const info = _discCache.get(url) || {status: 'checking', runName: null, systemName: null};
  item.className = 'disc-item disc-' + info.status;
  const dot = item.querySelector('.disc-dot');
  if (dot) dot.className = 'disc-dot disc-' + info.status;
  const metaEl = item.querySelector('.disc-meta');
  if (metaEl) {
    const text = info.runName || info.systemName || '';
    metaEl.textContent = text;
    metaEl.title = text;
  }
}

function _discMakeItem(url) {
  const info = _discCache.get(url) || {status: 'checking', runName: null, systemName: null};
  const shortHost = url.replace(/^https?:\/\//, '').split('.')[0];
  const item = document.createElement('div');
  item.className = 'disc-item disc-' + info.status;
  item.dataset.discUrl = url;
  item.title = url;
  const dot = document.createElement('span');
  dot.className = 'disc-dot disc-' + info.status;
  item.appendChild(dot);
  const labelEl = document.createElement('span');
  labelEl.className = 'disc-label';
  labelEl.textContent = shortHost;
  item.appendChild(labelEl);
  const metaEl = document.createElement('span');
  metaEl.className = 'disc-meta';
  const text = info.runName || info.systemName || '';
  metaEl.textContent = text;
  metaEl.title = text;
  item.appendChild(metaEl);
  return item;
}

function _discShow() {
  if (!_sourceDropdown || !_sourceDropdown.hidden) return;
  _sourceDropdown.innerHTML = '';
  for (const group of _DISC_GROUPS) {
    const lbl = document.createElement('div');
    lbl.className = 'disc-group-label';
    lbl.textContent = group.label;
    _sourceDropdown.appendChild(lbl);
    for (const url of group.urls) _sourceDropdown.appendChild(_discMakeItem(url));
  }
  _sourceDropdown.hidden = false;
  for (const url of _DISC_HOSTS) {
    if (!_discCache.has(url) || _discCache.get(url).status === 'checking') _discProbe(url);
  }
}

function _discHide() {
  if (_sourceDropdown) _sourceDropdown.hidden = true;
}

if (_sourceDropdown) {
  _sourceDropdown.addEventListener('click', e => {
    const item = e.target.closest('[data-disc-url]');
    if (!item) return;
    const url = item.dataset.discUrl;
    if ((_discCache.get(url) || {}).status === 'available') {
      sourceInput.value = url;
      _sourceInputTouched = true;
      _discHide();
      switchSource();
    }
  });
}

sourceInput.addEventListener('focus', _discShow);
sourceInput.addEventListener('keydown', e => { if (e.key === 'Escape') { _discHide(); e.stopPropagation(); } });
document.addEventListener('mousedown', e => {
  if (_sourceDropdown && !_sourceDropdown.hidden &&
      !sourceInput.contains(e.target) && !_sourceDropdown.contains(e.target)) {
    _discHide();
  }
});
