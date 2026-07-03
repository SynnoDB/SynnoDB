'use strict';

// Top-of-page rendering: header meta, the three KPI cards, the prompts list
// in the sidebar, and the correctness strip aligned to the timeline chart.

// ── Turn timer ───────────────────────────────────────────────────────────
let _timerTurn  = null;
let _timerStart = null;

function tickTimer() {
  const el = document.getElementById('v-timer');
  if (!el) return;
  // The run aborted - hold the timer at its last value instead of ticking up
  // forever on a run that is no longer making progress.
  if (_timerFrozen) return;
  if (_timerStart == null) { el.textContent = '—'; return; }
  const secs = Math.floor((Date.now() - _timerStart) / 1000);
  const m = Math.floor(secs / 60), s = secs % 60;
  el.textContent = m > 0 ? m + 'm ' + String(s).padStart(2,'0') + 's' : s + 's';
}

// ── Header ───────────────────────────────────────────────────────────────
function updateHeaderMeta(meta = {}) {
  if (meta.start_time) {
    const d = new Date(meta.start_time);
    document.getElementById('hdr-start-time').textContent =
      d.toLocaleDateString(undefined, {month:'short', day:'numeric'}) + ' ' +
      d.toLocaleTimeString(undefined, {hour:'2-digit', minute:'2-digit'});
  }
  document.getElementById('hdr-wandb-id').textContent   = meta.wandb_run_id || '—';
  document.getElementById('hdr-run-name').textContent   = meta.run_name     || '—';
  document.getElementById('hdr-system-name').textContent = meta.system_name || '—';

  const isStandalone = meta._source_type === 'db' || meta._source_type === 'wandb' ||
                       meta._source_type === 'remote' || meta._source_type === 'standalone';
  const modeLabel = isStandalone ? 'Standalone' : 'Live';
  document.title = '[' + modeLabel + '] SynnoDB — Live Dashboard';

  document.getElementById('hdr').classList.toggle('standalone', isStandalone);

  let badge = document.getElementById('hdr-mode-badge');
  if (!badge) {
    badge = document.createElement('span');
    badge.id = 'hdr-mode-badge';
    const title = document.getElementById('hdr-title');
    title.parentNode.insertBefore(badge, title.nextSibling);
  }
  badge.textContent = modeLabel;
  badge.className = 'mode-badge ' + (isStandalone ? 'mode-standalone' : 'mode-live');

  // The code inspector only has a workspace to serve when a live dashboard drain
  // backs the source: the in-process live run (no _source_type) or a proxied
  // remote live dashboard. DuckDB / W&B sources have no live workspace, so hide
  // the button (and close the modal if it is open) for them.
  const codeBtn = document.getElementById('code-btn');
  if (codeBtn) {
    const codeAvailable = !meta._source_type || meta._source_type === 'remote';
    codeBtn.hidden = !codeAvailable;
    if (!codeAvailable) {
      const codeModal = document.getElementById('code-modal');
      if (codeModal && !codeModal.hidden) codeModal.hidden = true;
    }
  }
}

// ── Cost mode toggle (calculatorial vs after-cache) ──────────────────────
function setCostMode(mode) {
  costMode = mode;
  document.querySelectorAll('.cost-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
  document.getElementById('cost-label').textContent = mode === 'real' ? 'USD after caching' : 'USD Calculatorial';
  if (_lastSteps.length) updateCards(_lastSteps, _lastData);
}

document.getElementById('cost-mode-toggle').addEventListener('click', e => {
  const btn = e.target.closest('.cost-btn');
  if (btn) setCostMode(btn.dataset.mode);
});

// ── Cards ────────────────────────────────────────────────────────────────
function updateCards(steps, data) {
  const last = steps.length ? steps[steps.length-1] : null;
  const d = last != null ? (data[last] || {}) : {};
  document.getElementById('v-turn').textContent = last ?? '—';
  const costKey = costMode === 'real' ? 'total/real_cost_usd' : 'total/cost_usd';
  const costEntry = [...steps].reverse().find(s => (data[s] || {})[costKey] != null);
  document.getElementById('v-cost').textContent = fmtCost(costEntry != null ? data[costEntry][costKey] : null);
  document.getElementById('v-time').textContent = fmtTime(d['total/runtime'] ?? null);
  if (last !== _timerTurn) { _timerTurn = last; _timerStart = last != null ? Date.now() : null; }
  tickTimer();
}

// Maps section desc → full prompt text / agent config, populated by updatePrompts.
const _promptsByDesc = new Map();
const _configByDesc  = new Map();
// Descriptors currently rendered as not-yet-executed (scheduled) stages. Their
// prompt text in _promptsByDesc is a best-effort preview, so the modal flags it.
const _futurePrompts = new Set();

// Determine which of the running conversation's scheduled stages have not been
// reached yet. The backend publishes the full planned stage list of the active
// conversation as meta.planned_stages; we align it against the executed sections
// (which span the whole pipeline) by walking a monotonic pointer through the
// plan and consuming an entry whenever an executed section matches its
// descriptor. Off-plan sections (correctness retries, supervisor feedback) never
// match and so never advance the pointer. Everything past the last match is
// still upcoming.
//
// A `dynamic` planned entry (a PerQueryLoop) is special: it emits its concrete
// work as many inner per-query sections under runtime descriptors we cannot
// predict, so none of them ever equals the loop's own descriptor. Matching by
// descriptor alone would leave the loop entry stranded in "Scheduled" for the
// whole time it is actually running (and forever if no later planned stage
// follows to sweep the pointer past it). So when the pointer sits on a dynamic
// entry and a section arrives that matches nothing from here on, we treat that
// as the dynamic stage having started and consume its entry - its inner
// sections carry the live display from then on, while any planned stage after
// the loop stays scheduled until its own descriptor executes.
function getFutureStages(steps, sections) {
  const planned = _lastMeta && _lastMeta.planned_stages;
  const stages  = planned && Array.isArray(planned.stages) ? planned.stages : [];
  if (!stages.length) return [];

  const base = planned.base_step;
  const curSecs = base == null
    ? sections
    : sections.filter(sec => steps[sec.startIdx] >= base);

  let p = 0;
  for (const sec of curSecs) {
    let m = p;
    while (m < stages.length && stages[m].descriptor !== sec.desc) m++;
    if (m < stages.length) p = m + 1;
    else if (p < stages.length && stages[p].dynamic) p += 1;
  }
  return stages.slice(p);
}

// ── Prompts list (per-section turn / time / cost summary + upcoming stages) ─
function updatePrompts(steps, data) {
  const sections = getSections(steps, data);
  const future   = getFutureStages(steps, sections);

  const el = document.getElementById('prompt-list');
  if (!sections.length && !future.length) {
    el.innerHTML = '<div class="pl-item" style="color:var(--muted)">No stages yet…</div>';
    return;
  }

  const valueAtOrBefore = (endIdx, key, fallback = null) => {
    for (let idx = Math.min(endIdx, steps.length - 1); idx >= 0; idx--) {
      const v = (data[steps[idx]] || {})[key];
      if (v != null) return v;
    }
    return fallback;
  };
  const valueBefore = (startIdx, key, fallback = 0) => valueAtOrBefore(startIdx - 1, key, fallback);

  _promptsByDesc.clear();
  _configByDesc.clear();
  _futurePrompts.clear();
  for (const sec of sections) {
    const promptText = (data[steps[sec.startIdx]] || {}).current_prompt || null;
    if (promptText) _promptsByDesc.set(sec.desc, promptText);
    const configRaw = (data[steps[sec.startIdx]] || {}).agent_config || null;
    if (configRaw) _configByDesc.set(sec.desc, parseJsonField(configRaw));
  }

  const costKey = costMode === 'real' ? 'total/real_cost_usd' : 'total/cost_usd';
  const executedHtml = sections.map(sec => {
    const desc      = sec.desc;
    const active    = sec.endIdx >= steps.length - 1 ? ' active' : '';
    const color     = sectRgba(desc, 0.9);
    const firstStep = steps[sec.startIdx];
    const lastStep  = steps[sec.endIdx];
    const costEnd   = valueAtOrBefore(sec.endIdx, costKey, null);
    const timeEnd   = valueAtOrBefore(sec.endIdx, 'total/runtime', null);
    const costPrev  = valueBefore(sec.startIdx, costKey, 0);
    const timePrev  = valueBefore(sec.startIdx, 'total/runtime', 0);
    const tStr   = fmtTime(timeEnd != null ? timeEnd - timePrev : null);
    const cStr   = fmtCost(costEnd != null ? costEnd - costPrev : null);
    const turnStr = firstStep === lastStep ? `turn ${firstStep}` : `turn ${firstStep}-${lastStep}`;
    return `<div class="pl-item${active}" data-desc="${esc(desc)}" data-first="${firstStep}" data-last="${lastStep}">
      <div class="pl-name" style="border-left-color:${color}">${esc(desc)}</div>
      <div class="pl-meta">${turnStr} &nbsp;·&nbsp; ${tStr} &nbsp;·&nbsp; ${cStr}</div>
    </div>`;
  }).join('');

  // Scheduled, not-yet-executed stages of the running conversation.
  const futureHtml = future.map(fs => {
    const desc = fs.descriptor;
    _futurePrompts.add(desc);
    if (!_promptsByDesc.has(desc)) {
      _promptsByDesc.set(
        desc,
        fs.prompt_preview ||
          '_The prompt for this scheduled stage is generated at runtime and is not known yet._'
      );
    }
    const note = fs.dynamic
      ? 'scheduled · prompt built at runtime'
      : fs.has_runtime_placeholder
        ? 'scheduled · runtime values pending'
        : 'scheduled';
    return `<div class="pl-item pl-future" data-desc="${esc(desc)}" data-future="1">
      <div class="pl-name">${esc(desc)}</div>
      <div class="pl-meta">${note}</div>
    </div>`;
  }).join('');

  const futureHeader = future.length
    ? '<div class="pl-section-header">Scheduled</div>'
    : '';

  el.innerHTML = executedHtml + futureHeader + futureHtml;
}

// ── Correctness strip ────────────────────────────────────────────────────
function updateCorrectness(steps, data) {
  const row = document.getElementById('corr-row');
  let state = null;
  row.innerHTML = steps.map(s => {
    const c = (data[s] || {})['validation/correct'];
    if (c === true) state = 'ok';
    else if (c === false) state = 'err';
    const cls = state ?? 'na';
    const lbl = cls === 'ok' ? '✓ correct' : cls === 'err' ? '✗ incorrect' : 'n/a';
    return `<div class="corr ${cls}" data-step="${s}" title="Turn ${s}: ${lbl}"></div>`;
  }).join('');
  layoutCorrectnessWithChart();
}

// Pin each correctness marker under its corresponding x-position on the
// timeline chart. Triggered on data updates and from the correctnessAlign
// Chart.js plugin so resizes stay in sync.
function layoutCorrectnessWithChart(activeChart = chart) {
  const row = document.getElementById('corr-row');
  if (!row) return;

  const markers = [...row.querySelectorAll('.corr')];
  if (!markers.length) {
    row.style.marginLeft = '';
    row.style.marginRight = '';
    row.style.width = '';
    return;
  }

  const chartArea = activeChart?.chartArea;
  const xScale    = activeChart?.scales?.x;
  const points    = activeChart?._timelinePoints;
  if (!chartArea || !xScale || !points?.length) {
    row.style.marginLeft = '';
    row.style.marginRight = '';
    row.style.width = '';
    return;
  }

  const plotWidth = Math.max(0, chartArea.right - chartArea.left);
  row.style.marginLeft = `${chartArea.left}px`;
  row.style.marginRight = '';
  row.style.width = `${plotWidth}px`;

  markers.forEach((marker, idx) => {
    const bounds = getSegmentBounds(points, idx);
    if (!bounds) return;
    const left  = xScale.getPixelForValue(bounds.left)  - chartArea.left;
    const right = xScale.getPixelForValue(bounds.right) - chartArea.left;
    marker.style.left  = `${left}px`;
    marker.style.width = `${Math.max(2, right - left)}px`;
  });
}
