'use strict';

// Wires up the rest of the chrome: chart-mode toggles, prompt-list hover,
// panel collapse, the time-distribution modal, time-travel reset, and the
// keyboard shortcut for closing modals / leaving time-travel.

// ── Prompt-list hover → highlight chart section + log entries ────────────
document.getElementById('prompt-list').addEventListener('mouseover', e => {
  const item = e.target.closest('.pl-item[data-desc]');
  if (!item) return;
  setHoveredSection(item.dataset.desc, +item.dataset.first, +item.dataset.last);
});
document.getElementById('prompt-list').addEventListener('mouseleave', () => {
  setHoveredSection(null, null, null);
});

// ── Prompt-list click → show full prompt in modal ────────────────────────
const promptModal      = document.getElementById('prompt-modal');
const promptModalClose = document.getElementById('prompt-modal-close');
const promptModalTitle = document.getElementById('prompt-modal-title');
const promptModalBody  = document.getElementById('prompt-modal-body');

function openPromptModal(desc) {
  const text = _promptsByDesc.get(desc);
  if (!text) return;
  promptModalTitle.textContent = desc;
  promptModalBody.textContent  = text;
  promptModal.hidden = false;
}

document.getElementById('prompt-list').addEventListener('click', e => {
  const item = e.target.closest('.pl-item[data-desc]');
  if (!item) return;
  openPromptModal(item.dataset.desc);
});
promptModalClose.addEventListener('click', () => { promptModal.hidden = true; });
promptModal.addEventListener('click', e => { if (e.target === promptModal) promptModal.hidden = true; });

// ── Time distribution modal (pie / stacked / call-count) ─────────────────
const distToggle     = document.getElementById('dist-toggle');
const distModal      = document.getElementById('dist-modal');
const distModalClose = document.getElementById('dist-modal-close');

function applyDistChartMode(mode) {
  distChartMode = mode;
  document.querySelectorAll('.dcm-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
  const pcCanvas     = document.getElementById('pc');
  const tcDistCanvas = document.getElementById('tc-dist');
  const showPie      = mode === 'pie';
  pcCanvas.style.display     = showPie ? '' : 'none';
  tcDistCanvas.style.display = showPie ? 'none' : '';
  if (showPie) {
    setTimeout(() => { if (pieChart) pieChart.resize(); }, 50);
    updatePieChart(_lastSteps, _lastData);
  } else {
    setTimeout(() => { if (timelineDistChart) timelineDistChart.resize(); }, 50);
    updateTimelineDistChart(_lastSteps, _lastData, mode);
  }
}

document.getElementById('dist-chart-toggle').addEventListener('click', e => {
  const btn = e.target.closest('.dcm-btn');
  if (btn) applyDistChartMode(btn.dataset.mode);
});

distToggle.addEventListener('click', () => {
  distModal.hidden = false;
  applyDistChartMode(distChartMode);
  updateBarChart(_lastSteps, _lastData);
});
distModalClose.addEventListener('click', () => { distModal.hidden = true; });
distModal.addEventListener('click', e => { if (e.target === distModal) distModal.hidden = true; });

// Esc closes the topmost modal first, then drops out of time-travel mode.
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (!promptModal.hidden) promptModal.hidden = true;
    else if (!distModal.hidden) distModal.hidden = true;
    else if (timeTravelStep != null) setTimeTravelStep(null);
  }
});

document.getElementById('timetravel-live-btn').addEventListener('click', () => {
  setTimeTravelStep(null);
});

// ── Panel collapse toggles ───────────────────────────────────────────────
const sidebarToggle = document.getElementById('sidebar-toggle');
const logToggle     = document.getElementById('log-toggle');
const sidebar       = document.getElementById('sidebar');
const logPanel      = document.getElementById('log-panel');
const sidebarClose  = document.getElementById('sidebar-close');
const logClose      = document.getElementById('log-close');

function resizeCharts() {
  setTimeout(() => {
    if (chart) chart.resize();
    if (queryChart) queryChart.resize();
  }, 200);
}

function syncPanelToggles() {
  const sidebarCollapsed = sidebar.classList.contains('collapsed');
  const logCollapsed     = logPanel.classList.contains('collapsed');

  sidebarToggle.setAttribute('aria-expanded', String(!sidebarCollapsed));
  logToggle.setAttribute('aria-expanded', String(!logCollapsed));
  logToggle.classList.toggle('show', logCollapsed);
}

sidebarToggle.addEventListener('click', () => {
  sidebar.classList.toggle('collapsed');
  syncPanelToggles();
  resizeCharts();
});
logToggle.addEventListener('click', () => {
  logPanel.classList.toggle('collapsed');
  syncPanelToggles();
  resizeCharts();
});
sidebarClose.addEventListener('click', () => {
  sidebar.classList.add('collapsed');
  syncPanelToggles();
  resizeCharts();
});
logClose.addEventListener('click', () => {
  logPanel.classList.add('collapsed');
  syncPanelToggles();
  resizeCharts();
});

// ── Chart-mode toggles (timeline turn/time, query speedup/absolute) ──────
function setTimelineChartMode(mode) {
  timelineChartMode = mode;
  document.querySelectorAll('.tlm-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === mode);
  });
  updateChart(_lastSteps, _lastData);
}

document.getElementById('timeline-mode-toggle').addEventListener('click', e => {
  const btn = e.target.closest('.tlm-btn');
  if (btn) setTimelineChartMode(btn.dataset.mode);
});

function setQueryChartMode(mode) {
  queryChartMode = mode;
  document.querySelectorAll('.qcm-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === mode);
  });
  updateQueryChart(_lastSteps, _lastData);
}

document.getElementById('qc-mode-toggle').addEventListener('click', e => {
  const btn = e.target.closest('.qcm-btn');
  if (btn) setQueryChartMode(btn.dataset.mode);
});

document.getElementById('qc-sf-toggle').addEventListener('click', e => {
  const btn = e.target.closest('.sf-btn');
  if (!btn) return;
  const sf = Number(btn.dataset.sf);
  selectedScaleFactor = Number.isFinite(sf) ? sf : null;
  updateScaleFactorButtons(_lastSteps, _lastData);
  updateChart(_lastSteps, _lastData);
  updateQueryChart(_lastSteps, _lastData);
});
