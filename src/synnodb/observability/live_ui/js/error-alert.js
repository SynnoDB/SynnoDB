'use strict';

// Run-error surface. The backend sets meta.error (message + traceback + log
// file) when a run aborts with an unrecovered exception. poll() hands that
// object here every tick: we raise a persistent banner, flip the header status
// to a red error state, freeze the per-turn timer, and populate a details modal
// with the full traceback so the user can "check the log" without leaving the
// dashboard.

// Signature of the error currently displayed, so repeated polls don't re-open a
// banner the user has dismissed. A genuinely new error (different time/message)
// resets the dismissal and shows again.
let _shownErrorSig = null;
let _dismissedErrorSig = null;
let _currentError = null;

function _errorSig(err) {
  return (err.time || '') + '|' + (err.message || '');
}

// Called from poll() with meta.error (or a falsy value when the run is healthy).
function updateRunError(err) {
  if (!err) { clearRunError(); return; }

  _currentError = err;
  const sig = _errorSig(err);

  // Header status: red dot + "run stopped" text, re-applied every poll so it
  // wins over the normal "Updated …" status set earlier in the tick.
  const dot = document.querySelector('.dot');
  if (dot) { dot.classList.add('error'); dot.classList.remove('cached'); }
  const ts = document.getElementById('ts-txt');
  if (ts) { ts.textContent = 'Error — run stopped'; ts.classList.add('ts-error'); }

  // Stop the clock on the dead run.
  _timerFrozen = true;

  // Keep the details modal live if it is open on this same error.
  if (!document.getElementById('error-modal').hidden) fillErrorModal(err);

  if (sig === _shownErrorSig) return;  // already surfaced this exact error
  _shownErrorSig = sig;

  const banner = document.getElementById('error-banner');
  document.getElementById('error-banner-msg').textContent = _oneLine(err.message);
  // A new, not-yet-dismissed error pops the banner open.
  if (sig !== _dismissedErrorSig) banner.hidden = false;
}

function clearRunError() {
  _currentError = null;
  _shownErrorSig = null;
  _dismissedErrorSig = null;
  _timerFrozen = false;

  const banner = document.getElementById('error-banner');
  if (banner) banner.hidden = true;
  const modal = document.getElementById('error-modal');
  if (modal) modal.hidden = true;

  const dot = document.querySelector('.dot');
  if (dot) dot.classList.remove('error');
  const ts = document.getElementById('ts-txt');
  if (ts) ts.classList.remove('ts-error');
}

// Collapse a multi-line message to a single trimmed line for the banner.
function _oneLine(msg) {
  const s = String(msg || '').trim().split('\n')[0];
  return s.length > 200 ? s.slice(0, 198) + '…' : s;
}

function fillErrorModal(err) {
  document.getElementById('error-modal-msg').textContent = err.message || '(no message)';

  const logEl = document.getElementById('error-modal-logfile');
  if (err.log_file) {
    logEl.textContent = 'Full log: ' + err.log_file;
    logEl.hidden = false;
  } else {
    logEl.hidden = true;
  }

  document.getElementById('error-modal-tb').textContent =
    err.traceback || '(no traceback captured)';
}

function openErrorModal() {
  if (!_currentError) return;
  fillErrorModal(_currentError);
  document.getElementById('error-modal').hidden = false;
}

// ── Wiring ────────────────────────────────────────────────────────────────
const _errBanner  = document.getElementById('error-banner');
const _errModal   = document.getElementById('error-modal');
const _errModalTb = () => document.getElementById('error-modal-tb').textContent;

document.getElementById('error-banner-details').addEventListener('click', openErrorModal);
document.getElementById('error-banner-dismiss').addEventListener('click', () => {
  _errBanner.hidden = true;
  if (_currentError) _dismissedErrorSig = _errorSig(_currentError);
});

document.getElementById('error-modal-close').addEventListener('click', () => { _errModal.hidden = true; });
_errModal.addEventListener('click', e => { if (e.target === _errModal) _errModal.hidden = true; });

document.getElementById('error-modal-copy').addEventListener('click', async () => {
  const copyBtn = document.getElementById('error-modal-copy');
  const text = _errModalTb();
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  copyBtn.textContent = 'Copied!';
  setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500);
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !_errModal.hidden) _errModal.hidden = true;
});
