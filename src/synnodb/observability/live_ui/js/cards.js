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

  // The serving thread count is a run-level constant published as run metadata;
  // hide the row until the run has reported it.
  const threadsRow = document.getElementById('hdr-threads-item');
  const nt = meta.num_threads;
  if (nt != null) {
    document.getElementById('hdr-num-threads').textContent = Number.isFinite(+nt) ? +nt : nt;
    threadsRow.hidden = false;
  } else {
    threadsRow.hidden = true;
  }

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

// Surface the model driving the run in the header. The backend lifts it out of
// the newest agent_config into meta.model (the per-step configs are lazily
// served and absent from the snapshot); hide the chip until one is known.
function updateHeaderModel() {
  const el  = document.getElementById('hdr-model');
  const val = document.getElementById('hdr-model-val');
  if (!el || !val) return;
  const model = _lastMeta && _lastMeta.model;
  if (model) {
    val.textContent = model;
    val.title = model;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

// ── Run info panel (started / W&B / run / system / reloaded) ──────────────
(function initHeaderInfo() {
  const wrap  = document.getElementById('hdr-info-wrap');
  const btn   = document.getElementById('hdr-info-btn');
  const panel = document.getElementById('hdr-info-panel');
  if (!wrap || !btn || !panel) return;

  function close() { panel.hidden = true; btn.setAttribute('aria-expanded', 'false'); }
  function open()  { panel.hidden = false; btn.setAttribute('aria-expanded', 'true'); }

  btn.addEventListener('click', e => {
    e.stopPropagation();
    if (panel.hidden) open(); else close();
  });
  panel.addEventListener('click', e => e.stopPropagation());
  document.addEventListener('click', e => {
    if (!panel.hidden && !wrap.contains(e.target)) close();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !panel.hidden) close();
  });
})();

// ── Cost mode toggle (calculatorial vs after-cache) ──────────────────────
function setCostMode(mode) {
  costMode = mode;
  document.querySelectorAll('.cost-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
  document.getElementById('cost-label').textContent = mode === 'real' ? 'USD after caching' : 'USD w/o local LLM cache';
  if (_lastSteps.length) updateCards(_lastSteps, _lastData);
}

document.getElementById('cost-mode-toggle').addEventListener('click', e => {
  const btn = e.target.closest('.cost-btn');
  if (btn) setCostMode(btn.dataset.mode);
});

// ── Cards ────────────────────────────────────────────────────────────────
function updateCards(steps, data) {
  updateHeaderModel();
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

// Maps section desc → the step whose row carries the section's prompt text and
// agent config (its first step). The text itself is lazily served — the modal
// fetches it from /api/step_body on open. Populated by updatePrompts.
const _promptStepByDesc = new Map();
// desc → preview text for scheduled (not-yet-executed) stages, straight from
// meta.planned_stages — inline, no fetch needed.
const _futurePreviewByDesc = new Map();
// Descriptors currently rendered as not-yet-executed (scheduled) stages. Their
// preview text is best-effort, so the modal flags it.
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

  _promptStepByDesc.clear();
  _futurePreviewByDesc.clear();
  _futurePrompts.clear();
  for (const sec of sections) {
    _promptStepByDesc.set(sec.desc, steps[sec.startIdx]);
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
    if (!_promptStepByDesc.has(desc)) {
      _futurePreviewByDesc.set(
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
// The carried-forward verdict rarely changes from one step to the next, so the
// strip is stored and rendered as run-length segments: one <div> per contiguous
// run of the same verdict. A long run yields a handful of segments where
// per-step markers used to pile up thousands of DOM nodes, every one of which
// had to be repositioned on each chart layout. Ingestion is incremental; the
// newest ingested step is rolled back and re-read on every update because its
// turn can still be accumulating fields (its validation verdict may arrive on
// a later poll). A diverging prefix (source switch / timeline reset) rebuilds
// from scratch.
let _corrSegs = [];        // {cls, startIdx, endIdx, el}, in step order
let _corrState = null;     // carried-forward verdict: 'ok' | 'err' | null
let _corrCount = 0;        // steps ingested
let _corrFirstStep = null;
let _corrLastStep = null;
let _corrFirstRow = null;  // identity of data[steps[0]] at last ingest

function _corrReset(row) {
  row.innerHTML = '';
  _corrSegs = [];
  _corrState = null;
  _corrCount = 0;
  _corrFirstStep = null;
  _corrLastStep = null;
  _corrFirstRow = null;
}

function _corrSegTitle(seg, steps) {
  const lbl = seg.cls === 'ok' ? '✓ correct' : seg.cls === 'err' ? '✗ incorrect' : 'n/a';
  const a = steps[seg.startIdx], b = steps[seg.endIdx];
  return (a === b ? `Turn ${a}` : `Turns ${a}-${b}`) + `: ${lbl}`;
}

// Drop ingested steps at index >= k from the segments so they can be re-read.
function _corrRollbackTo(k, steps) {
  while (_corrSegs.length && _corrSegs[_corrSegs.length - 1].startIdx >= k) {
    _corrSegs.pop().el.remove();
  }
  const last = _corrSegs[_corrSegs.length - 1];
  if (last && last.endIdx >= k) {
    last.endIdx = k - 1;
    last.el.title = _corrSegTitle(last, steps);
  }
  _corrState = last && last.cls !== 'na' ? last.cls : null;
}

function updateCorrectness(steps, data) {
  const row = document.getElementById('corr-row');

  // Step ids alone cannot detect a generation reset (numbering restarts at 0),
  // so the first row's object identity is checked too: every store replacement
  // swaps it, while delta polls never re-send it once it stops being the
  // newest step.
  const diverged = steps.length < _corrCount ||
    (_corrCount > 0 && (String(steps[_corrCount - 1]) !== _corrLastStep ||
                        String(steps[0]) !== _corrFirstStep ||
                        data[steps[0]] !== _corrFirstRow));
  if (diverged) _corrReset(row);
  else if (_corrCount > 0) { _corrRollbackTo(_corrCount - 1, steps); _corrCount -= 1; }

  for (let i = _corrCount; i < steps.length; i++) {
    const c = (data[steps[i]] || {})['validation/correct'];
    if (c === true) _corrState = 'ok';
    else if (c === false) _corrState = 'err';
    const cls = _corrState ?? 'na';
    const last = _corrSegs[_corrSegs.length - 1];
    if (last && last.cls === cls) {
      last.endIdx = i;
      last.el.title = _corrSegTitle(last, steps);
    } else {
      const el = document.createElement('div');
      el.className = 'corr ' + cls;
      const seg = {cls, startIdx: i, endIdx: i, el};
      el.title = _corrSegTitle(seg, steps);
      row.appendChild(el);
      _corrSegs.push(seg);
    }
  }

  _corrCount = steps.length;
  _corrFirstStep = steps.length ? String(steps[0]) : null;
  _corrLastStep = steps.length ? String(steps[steps.length - 1]) : null;
  _corrFirstRow = steps.length ? data[steps[0]] : null;
  layoutCorrectnessWithChart();
}

// Pin each correctness segment under its x-range on the timeline chart.
// Triggered on data updates and from the correctnessAlign Chart.js plugin so
// resizes stay in sync.
function layoutCorrectnessWithChart(activeChart = chart) {
  const row = document.getElementById('corr-row');
  if (!row) return;

  const chartArea = activeChart?.chartArea;
  const xScale    = activeChart?.scales?.x;
  const points    = activeChart?._timelinePoints;
  if (!_corrSegs.length || !chartArea || !xScale || !points?.length) {
    row.style.marginLeft = '';
    row.style.marginRight = '';
    row.style.width = '';
    return;
  }

  const plotWidth = Math.max(0, chartArea.right - chartArea.left);
  row.style.marginLeft = `${chartArea.left}px`;
  row.style.marginRight = '';
  row.style.width = `${plotWidth}px`;

  for (const seg of _corrSegs) {
    const startBounds = getSegmentBounds(points, seg.startIdx);
    const endBounds   = getSegmentBounds(points, seg.endIdx);
    if (!startBounds || !endBounds) continue;
    const left  = xScale.getPixelForValue(startBounds.left) - chartArea.left;
    const right = xScale.getPixelForValue(endBounds.right)  - chartArea.left;
    seg.el.style.left  = `${left}px`;
    seg.el.style.width = `${Math.max(2, right - left)}px`;
  }
}
