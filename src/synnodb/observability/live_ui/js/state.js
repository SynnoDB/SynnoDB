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

let selectedScaleFactor = null;  // null = follow max SF; else fixed SF

// The _source_ref the user has explicitly switched to. Used to drop stale
// /api/stats responses that were already in flight when the switch happened —
// without this guard the dashboard briefly flickers back to the previous run.
let _expectedSourceRef = null;
