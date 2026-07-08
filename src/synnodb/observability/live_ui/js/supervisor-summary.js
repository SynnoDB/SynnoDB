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
  if (!entries.length) { panel.hidden = true; return; }
  panel.hidden = false;

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
