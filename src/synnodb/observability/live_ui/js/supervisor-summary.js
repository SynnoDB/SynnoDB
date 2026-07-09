'use strict';

// Supervisor summary panel: sits at the bottom of the Activity Log bar.
// Shows the latest supervisor run summary (and dev hints, if enabled), with
// prev/next arrows to page through older supervisor turns.
//
// Cycling model: `_supPinnedStep` is null while following the newest entry
// live. Clicking "older" pins to the previous entry's step (a stable step id,
// not an index, so it doesn't drift under the user while new entries keep
// arriving). Clicking "newer" advances toward the newest entry; landing on it
// clears the pin and resumes live-follow — mirroring the timeline chart's
// time-travel-then-"Live" behavior without needing a separate button.
let _supPinnedStep = null;

function _supEntries(steps, data) {
  const out = [];
  for (const s of steps) {
    const d = data[s] || {};
    if (d['supervisor'] && d['supervisor/summary']) {
      out.push({ step: s, summary: d['supervisor/summary'], devHints: d['supervisor/dev_hints'] || null });
    }
  }
  return out;
}

function _supCurrentIndex(entries) {
  if (_supPinnedStep == null) return entries.length - 1;
  const idx = entries.findIndex(e => String(e.step) === String(_supPinnedStep));
  if (idx === -1) { _supPinnedStep = null; return entries.length - 1; }
  return idx;
}

function updateSupervisorSummary(steps, data) {
  const entries = _supEntries(steps, data);
  const panel = document.getElementById('sup-summary-panel');
  const resizer = document.getElementById('sup-resizer');
  if (!entries.length) { panel.hidden = true; resizer.hidden = true; return; }
  panel.hidden = false;
  resizer.hidden = false;

  const idx = _supCurrentIndex(entries);
  const entry = entries[idx];

  document.getElementById('sup-summary-text').textContent = entry.summary;
  document.getElementById('sup-summary-pos').textContent =
    '#' + entry.step + ' · ' + (idx + 1) + '/' + entries.length;

  const hintsEl = document.getElementById('sup-dev-hints');
  if (entry.devHints) {
    hintsEl.hidden = false;
    document.getElementById('sup-dev-hints-text').textContent = entry.devHints;
  } else {
    hintsEl.hidden = true;
  }

  document.getElementById('sup-prev').disabled = idx === 0;
  document.getElementById('sup-next').disabled = idx === entries.length - 1;
}

document.getElementById('sup-prev').addEventListener('click', () => {
  const entries = _supEntries(_lastSteps, _lastData);
  if (!entries.length) return;
  const idx = _supCurrentIndex(entries);
  if (idx > 0) _supPinnedStep = entries[idx - 1].step;
  updateSupervisorSummary(_lastSteps, _lastData);
});

document.getElementById('sup-next').addEventListener('click', () => {
  const entries = _supEntries(_lastSteps, _lastData);
  if (!entries.length) return;
  const idx = _supCurrentIndex(entries);
  if (idx < entries.length - 1) {
    const newIdx = idx + 1;
    _supPinnedStep = (newIdx === entries.length - 1) ? null : entries[newIdx].step;
  }
  updateSupervisorSummary(_lastSteps, _lastData);
});

// Draggable divider between the activity log list and the supervisor panel.
// Dragging up grows the supervisor panel (and shrinks the log list, which
// flexes to fill the remaining space); dragging down shrinks it. The chosen
// height overrides the default CSS cap until the panel is next hidden.
(function initSupResizer() {
  const resizer = document.getElementById('sup-resizer');
  const panel = document.getElementById('sup-summary-panel');
  const MIN_H = 64;   // keep the header and a line of text visible
  const LIST_MIN = 120;  // never let the drag collapse the log list away

  function clamp(h) {
    const maxH = panel.parentElement.clientHeight - LIST_MIN;
    return Math.max(MIN_H, Math.min(h, Math.max(MIN_H, maxH)));
  }

  function onMove(e) {
    // Panel bottom is pinned to the bar's bottom edge, so the target height is
    // just the distance from the pointer up to that edge.
    const bottom = panel.getBoundingClientRect().bottom;
    panel.style.maxHeight = 'none';
    panel.style.height = clamp(bottom - e.clientY) + 'px';
    e.preventDefault();
  }

  function onUp() {
    document.body.classList.remove('sup-resizing');
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
  }

  resizer.addEventListener('pointerdown', (e) => {
    document.body.classList.add('sup-resizing');
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    e.preventDefault();
  });
})();
