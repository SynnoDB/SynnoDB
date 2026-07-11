'use strict';

// Boot: stamp the initial reload time, set default modes, init the two
// always-visible charts, then start polling /api/stats every 3s and ticking
// the per-turn timer every 1s.

document.getElementById('hdr-reload-time').textContent =
  new Date().toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'});

// Merge an incremental delta into the persistent store. The delta always
// carries the client's current max step (it may still be accumulating) plus any
// newer steps; updating in place keeps older, frozen steps untouched.
function mergeDelta(steps, data) {
  const known = new Set(_lastSteps.map(Number));
  let added = false;
  for (const s of steps || []) {
    const key = String(s);
    if (!known.has(Number(s))) { known.add(Number(s)); added = true; }
    _lastData[key] = data[key];
  }
  if (added) _lastSteps = [...known].sort((a, b) => a - b);
}

// Re-render every panel from the merged store. Only called when a poll actually
// changed something, so an idle run costs a single small fetch + string compare.
function renderAll() {
  updateCards(_lastSteps, _lastData);
  updatePrompts(_lastSteps, _lastData);
  updateCorrectness(_lastSteps, _lastData);
  updateScaleFactorButtons(_lastSteps, _lastData);
  updateChart(_lastSteps, _lastData);
  updateQueryChart(_lastSteps, _lastData);
  if (!distModal.hidden) {
    if (distChartMode === 'pie') updatePieChart(_lastSteps, _lastData);
    else updateTimelineDistChart(_lastSteps, _lastData, distChartMode);
    updateBarChart(_lastSteps, _lastData);
  }
  updateLog(_lastSteps, _lastData);
  updateSupervisorSummary(_lastSteps, _lastData);
}

async function poll() {
  try {
    const url = _pollCursor == null ? '/api/stats' : '/api/stats?since=' + _pollCursor;
    const r = await fetch(url);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const text = await r.text();
    // Byte-identical to what we last rendered → nothing changed; skip the parse
    // and the O(turns) chart rebuild. This is what keeps an idle long run cheap.
    if (text === _lastRespText) return;
    const payload = JSON.parse(text);
    const {meta, steps, data} = payload;
    // Drop responses for a source the user has already switched away from.
    // Without this, an in-flight /api/stats from before the switch can resolve
    // after the new data has rendered and briefly overwrites the UI with the
    // previous run's values.
    if (_expectedSourceRef != null && meta?._source_ref !== _expectedSourceRef) return;

    // A new pipeline in the same process resets the drain and restarts step
    // numbering from 0 under a fresh meta.start_time. Our cursor still points into
    // the previous run's numbering, so a delta would keep that run's lower-numbered
    // steps mixed into the store while the count guard below is fooled into parity.
    // Detect the generation change up front, wipe the store, and refetch full.
    const generation = meta?.start_time;
    if (generation != null && _runGeneration != null && generation !== _runGeneration) {
      _pollCursor = null;
      _lastRespText = null;
      _lastSteps = [];
      _lastData = {};
      _runGeneration = generation;
      return;
    }
    if (generation != null) _runGeneration = generation;

    // A full snapshot (no cursor) replaces the store; a delta updates in place.
    if (payload.incremental) mergeDelta(steps, data);
    else { _lastSteps = (steps || []).slice(); _lastData = data || {}; }

    // If our merged step count disagrees with the server's authoritative count we
    // have drifted (e.g. the run reset its timeline out from under us). Drop the
    // cursor so the next poll refetches a full snapshot from scratch.
    if (typeof payload.count === 'number' && _lastSteps.length !== payload.count) {
      _pollCursor = null;
      _lastRespText = null;
      return;
    }
    if (payload.latest != null) _pollCursor = payload.latest;
    _lastRespText = text;

    _lastMeta = meta || {};
    updateHeaderMeta(meta);
    updateSourceUI(meta);
    if (!_applyingInitialSource && !_initialSourceApplied && meta?._source_type === 'standalone') {
      if (await applyInitialSourceFromUrl()) return;
    }
    if (!_lastSteps.length) {
      document.getElementById('ts-txt').textContent = 'No data yet';
      // No turns emitted yet, but the running conversation may already have
      // published its scheduled stages - show them as upcoming.
      updatePrompts([], {});
      // Clear the query chart and surface its "no speedups yet" overlay rather
      // than leaving an empty panel (or stale bars after a source switch).
      updateQueryChart([], {});
      updateSupervisorSummary([], {});
      // A run can fail before emitting any turn (e.g. during setup); still
      // surface the error rather than sitting on "No data yet".
      updateRunError(meta && meta.error);
      return;
    }
    renderAll();
    document.getElementById('ts-txt').textContent = _tsTxt(meta);
    // Applied last so its red error status wins over the normal "Updated …"
    // header and freezes the timer when the run has aborted.
    updateRunError(meta && meta.error);
  } catch(e) {
    document.getElementById('ts-txt').textContent = 'Error: ' + e.message;
  }
}

setTimelineChartMode('turn');
setQueryChartMode('speedup');
syncPanelToggles();
initChart();
initQueryChart();

// Self-chaining poll loop: schedule the next poll only once the current one has
// settled. A fixed setInterval would let a slow render (or a large first
// snapshot) stack up overlapping fetches, each re-triggering a server-side
// serialize — the opposite of what we want on a long run.
(async function pollLoop() {
  try { await poll(); } finally { setTimeout(pollLoop, 3000); }
})();
setInterval(tickTimer, 1000);
