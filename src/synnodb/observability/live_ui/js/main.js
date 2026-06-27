'use strict';

// Boot: stamp the initial reload time, set default modes, init the two
// always-visible charts, then start polling /api/stats every 3s and ticking
// the per-turn timer every 1s.

document.getElementById('hdr-reload-time').textContent =
  new Date().toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'});

async function poll() {
  try {
    const r = await fetch('/api/stats');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const {meta, steps, data} = await r.json();
    // Drop responses for a source the user has already switched away from.
    // Without this, an in-flight /api/stats from before the switch can resolve
    // after the new data has rendered and briefly overwrites the UI with the
    // previous run's values.
    if (_expectedSourceRef != null && meta?._source_ref !== _expectedSourceRef) return;
    updateHeaderMeta(meta);
    updateSourceUI(meta);
    if (!_applyingInitialSource && !_initialSourceApplied && meta?._source_type === 'standalone') {
      if (await applyInitialSourceFromUrl()) return;
    }
    if (!steps?.length) {
      document.getElementById('ts-txt').textContent = 'No data yet';
      return;
    }
    _lastSteps = steps;
    _lastData  = data;
    updateCards(steps, data);
    updatePrompts(steps, data);
    updateCorrectness(steps, data);
    updateScaleFactorButtons(steps, data);
    updateChart(steps, data);
    updateQueryChart(steps, data);
    if (!distModal.hidden) {
      if (distChartMode === 'pie') updatePieChart(steps, data);
      else updateTimelineDistChart(steps, data, distChartMode);
      updateBarChart(steps, data);
    }
    updateLog(steps, data);
    document.getElementById('ts-txt').textContent = _tsTxt(meta);
  } catch(e) {
    document.getElementById('ts-txt').textContent = 'Error: ' + e.message;
  }
}

setTimelineChartMode('turn');
setQueryChartMode('speedup');
syncPanelToggles();
initChart();
initQueryChart();
poll();
setInterval(poll, 3000);
setInterval(tickTimer, 1000);
