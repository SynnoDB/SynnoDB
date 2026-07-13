'use strict';

// Shared mutable state across modules. Plain globals (no ES modules) so other
// scripts can read and assign them directly. Keep this list short — anything
// that's purely local to one module belongs in that module.

let _lastSteps = [];
let _lastData  = {};
let _lastMeta  = {};  // most recent /api/stats meta (holds planned_stages, etc.)

let chart             = null;  // main timeline chart
let queryChart        = null;  // per-query runtime bar chart
let pieChart          = null;  // wall-clock time pie
let timelineDistChart = null;  // wall-clock time stacked area
let barChart          = null;  // call-count bar

let timelineChartMode = 'turn';      // 'turn' | 'time'
let queryChartMode    = 'speedup';   // 'speedup' | 'absolute'
let costMode          = 'calc';      // 'calc'  | 'real'
let distChartMode     = 'pie';       // 'pie'   | 'rel' | 'abs'

let timeTravelStep = null;  // null = live, otherwise frozen turn id
let hoveredDesc    = null;  // section currently highlighted via hover

// Set once the backend reports the run aborted with an error. Freezes the
// per-turn timer so a dead run stops counting up. Cleared when a fresh run's
// data arrives without an error. See js/error-alert.js.
let _timerFrozen = false;

let selectedScaleFactor = null;  // null = follow max SF; else fixed SF

// The _source_ref the user has explicitly switched to. Used to drop stale
// /api/stats responses that were already in flight when the switch happened —
// without this guard the dashboard briefly flickers back to the previous run.
let _expectedSourceRef = null;

// Incremental polling cursor: the highest step id already merged into
// _lastSteps/_lastData. Sent as ?since=<cursor> so the server replies with only
// that step onward (the boundary step is re-sent because the current turn can
// still accumulate). null → request the full snapshot (first load / after a
// reset). See poll() in main.js.
let _pollCursor = null;
// Revision hash of the meta block held in _lastMeta. Echoed as ?meta_rev= so
// the server can omit the (comparatively large) meta from a delta whose meta
// has not changed; the client then keeps using _lastMeta. See poll() in main.js.
let _metaRev = null;
// Raw text of the last /api/stats response we rendered. A byte-identical
// response means nothing changed since the last render, so we skip the parse and
// the full chart rebuild entirely — the dominant idle cost on a long run.
let _lastRespText = null;
// The run's start_time (meta.start_time) of the generation our cursor belongs to.
// A new pipeline in the same process resets the drain and stamps a fresh
// start_time while restarting step numbering from 0, so a stale ?since cursor
// would fetch only the new run's boundary-onward steps and leave the previous
// run's lower-numbered steps mixed in. When start_time changes we discard the
// store and refetch a full snapshot. See poll() in main.js.
let _runGeneration = null;
