/*
 * Anastomosis Upload Console — vanilla JS, no frameworks, no build step.
 *
 * Inspects an existing tracking ledger AND drives the resumable upload engine
 * through the headless controller. Talks to the controller only:
 *   - upload_status(db)          → grouped state counters + run row + histograms
 *   - upload_manifest_preview(d) → count of renderable PDFs (no names)
 *   - upload_item_keys(db)       → pending item KEYS for the Cmd/Ctrl+K palette
 *   - upload_safety_notice()     → the shared-machine warning (single source)
 *   - upload_start(out,cdp,pack,packDirs,skiplist) → drive the engine over a loopback CDP attach
 *   - upload_stop()              → cooperative stop after the current document
 *
 * SECURITY: driving goes through upload_start / upload_stop ONLY. The JS never
 * touches the ledger's write surface — every ledger write (enqueue, state
 * changes, run bookkeeping) is owned by the controller, never issued from the
 * browser. The CDP attach is loopback-only (the controller hard-gates it) and
 * the browser is never closed by the GUI. While a run is in flight the live
 * counts come from POLLING upload_status against the ledger — the events carry
 * no counts, only stage/state/abort-TYPE names.
 *
 * The visual layer is carried from the predecessor (asymmetric counter grid,
 * calendar HUD with halo cells, command palette, log strip/drawer) via
 * window.AnastShell. The console mirrors the original app-shell most closely:
 * the counter grid is wired to OUR ledger state groups; the calendar plots
 * the run's start/finish days with the original halo treatment.
 *
 * PHI discipline: every value rendered is a count, a state name, a run/
 * destination id, an ISO timestamp, an exception TYPE name, or an opaque item
 * key (encounter id + hash). Never a patient name, never a file path.
 */
"use strict";

const Shell = window.AnastShell;
const CAL = { year: null, month: null, histogram: {} };
let ITEM_KEYS = [];
let PALETTE = null;
// The setInterval handle for the live-counter poll while an upload runs; null
// when no poll is active. The poll re-fetches upload_status against the ledger
// (counts come from the ledger, never from events).
let POLL_TIMER = null;
const POLL_INTERVAL_MS = 1500;

function hasApi() {
  return typeof window.pywebview !== "undefined" && !!window.pywebview.api;
}

function el(id) {
  return document.getElementById(id);
}

function setStatus(text) {
  const t = el("status-text");
  if (t) {
    t.textContent = text;
  }
}

function showBanner(message) {
  const banner = el("banner");
  if (banner) {
    banner.textContent = String(message);
    banner.classList.add("show");
  }
}

// --- inspect the ledger ---------------------------------------------------
async function onLoad() {
  if (!hasApi()) {
    return;
  }
  const dbPath = el("db-path").value;
  try {
    const status = await window.pywebview.api.upload_status(dbPath);
    if (!status || !status.ok) {
      showBanner("Could not read ledger: " + (status ? status.error : "no response"));
      Shell.logEvent({ kind: "error", msg: "ledger read failed" });
      return;
    }
    renderStatus(status);
    renderErrorHist(status.error_type_histogram || {});
    buildCalendarFromRun(status.run);
    el("run-panel").hidden = false;
    el("detail-panel").hidden = false;
    el("calendar-panel").hidden = false;
    Shell.logEvent({ kind: "ok", msg: "ledger inspected · total=" + (status.total || 0) });
  } catch (err) {
    showBanner(err);
  }

  const outDir = el("out-dir").value;
  if (outDir) {
    try {
      const preview = await window.pywebview.api.upload_manifest_preview(outDir);
      if (preview && preview.ok) {
        el("counter-renderable").textContent = String(preview.renderable);
        el("manifest-preview").textContent =
          "Manifest preview: " +
          preview.renderable +
          " renderable PDF(s), " +
          preview.total_bytes +
          " bytes total.";
      }
    } catch (err) {
      // Preview is advisory; never block the console on it.
    }
  }
}

function renderStatus(status) {
  const groups = status.groups || {};
  el("counter-terminal").textContent = String(groups.terminal || 0);
  el("counter-pending").textContent = String(groups.pending || 0);
  el("counter-active").textContent = String(groups.active || 0);
  el("counter-total").textContent = String(status.total || 0);
  const errorTypes = Object.keys(status.error_type_histogram || {}).length;
  el("counter-errortypes").textContent = String(errorTypes);

  const run = status.run;
  el("run-info").textContent = run ? "run " + run.run_id : "no runs";
  el("run-detail").innerHTML = "";
  const detail = document.createElement("div");
  detail.textContent = run
    ? "run " +
      run.run_id +
      " · " +
      run.destination +
      " · started " +
      run.started_at +
      (run.finished_at ? " · finished " + run.finished_at : " · in progress") +
      (run.aborted_reason ? " · aborted (" + run.aborted_reason + ")" : "")
    : "No runs recorded in this ledger.";
  el("run-detail").appendChild(detail);

  // Per-state breakdown (the 15 states, nonzero ones).
  const counts = status.counts || {};
  const grid = el("state-grid");
  grid.innerHTML = "";
  const states = Object.keys(counts).sort();
  if (states.length === 0) {
    grid.textContent = "No items enqueued.";
  }
  for (const state of states) {
    const cell = document.createElement("div");
    cell.className = "state-cell";
    const k = document.createElement("span");
    k.textContent = state;
    const v = document.createElement("span");
    v.className = "v";
    v.textContent = String(counts[state]);
    cell.appendChild(k);
    cell.appendChild(v);
    grid.appendChild(cell);
  }
}

// --- drive an upload (W5/PR-6b) -------------------------------------------
// Driving goes through the controller only. The JS never writes the ledger;
// the live counts come from polling upload_status (never from events).

// The ledger path the controller writes into: <out_dir>/upload_ledger.sqlite
// (the same file the CLI `anast upload` uses). The out-dir input is the single
// source dir (manifest + ledger).
function ledgerPath(outDir) {
  return outDir.replace(/\/+$/, "") + "/upload_ledger.sqlite";
}

// Render the shared-machine warning (the single source of truth from the
// controller) into the prominent #safety-warning element via textContent.
async function loadSafetyNotice() {
  if (!hasApi()) {
    return;
  }
  try {
    const res = await window.pywebview.api.upload_safety_notice();
    if (res && res.ok) {
      el("safety-warning").textContent = res.warning;
    }
  } catch (err) {
    // Advisory; never block the console on it.
  }
}

// Refresh the counter grid once from the ledger (reuses renderStatus).
async function refreshStatusOnce(dbPath) {
  if (!hasApi() || !dbPath) {
    return;
  }
  try {
    const status = await window.pywebview.api.upload_status(dbPath);
    if (status && status.ok) {
      renderStatus(status);
      renderErrorHist(status.error_type_histogram || {});
      el("run-panel").hidden = false;
      el("detail-panel").hidden = false;
    }
  } catch (err) {
    // Polling is advisory; a transient read failure must not break the run.
  }
}

function startPolling(dbPath) {
  stopPolling();
  POLL_TIMER = window.setInterval(() => {
    refreshStatusOnce(dbPath);
  }, POLL_INTERVAL_MS);
}

function stopPolling() {
  if (POLL_TIMER !== null) {
    window.clearInterval(POLL_TIMER);
    POLL_TIMER = null;
  }
}

async function onStartUpload() {
  if (!hasApi()) {
    return;
  }
  const outDir = el("out-dir").value;
  const cdpUrl = el("cdp-url").value;
  const packName = el("pack-name").value;
  if (!outDir || !cdpUrl || !packName) {
    showBanner("Provide an output directory, a loopback CDP endpoint, and a pack name.");
    return;
  }
  // Optional skiplist: one item key / encounter id per line (controller drops
  // blanks and "#" comments). Empty textarea -> no skiplist (null).
  const skiplistEl = el("skiplist");
  const skiplist = skiplistEl
    ? skiplistEl.value.split("\n").map((s) => s.trim()).filter((s) => s.length > 0)
    : [];
  setStatus("starting upload…");
  try {
    const res = await window.pywebview.api.upload_start(
      outDir,
      cdpUrl,
      packName,
      null,
      skiplist.length ? skiplist : null,
    );
    if (!res || !res.ok) {
      showBanner("Could not start upload: " + (res ? res.error : "no response"));
      setStatus("not started");
      Shell.logEvent({ kind: "error", msg: "upload start refused: " + (res ? res.error : "?") });
      return;
    }
    setStatus("uploading…");
    Shell.logEvent({ kind: "ok", msg: "upload started" });
    // The events carry stage/state names only; the live counts come from
    // polling the ledger while the run is in flight.
    startPolling(ledgerPath(outDir));
  } catch (err) {
    showBanner(err);
    setStatus("not started");
  }
}

async function onStopUpload() {
  if (!hasApi()) {
    return;
  }
  try {
    const res = await window.pywebview.api.upload_stop();
    if (res && res.ok) {
      setStatus("stopping after the current document…");
      Shell.logEvent({ kind: "info", msg: "stop requested (after current document)" });
    } else {
      setStatus("no run in flight");
    }
  } catch (err) {
    showBanner(err);
  }
}

// A terminal upload event (done or error): stop polling, do one final refresh
// against the ledger, and set the closing status.
function onUploadTerminal(finalStatus) {
  stopPolling();
  const outDir = el("out-dir").value;
  if (outDir) {
    refreshStatusOnce(ledgerPath(outDir));
  }
  setStatus(finalStatus);
}

// The event channel the shell pushes controller events into. Counts never ride
// events — only stage/state names and (on abort/failure) the exception TYPE.
window.anastEvent = function anastEvent(e) {
  if (!e || typeof e !== "object") {
    return;
  }
  switch (e.type) {
    case "stage":
      if (e.stage === "upload" && e.state === "done") {
        onUploadTerminal("upload complete");
        Shell.logEvent({ kind: "ok", msg: "upload complete" });
      } else if (e.stage === "upload") {
        setStatus("stage upload: " + e.state);
      }
      break;
    case "error":
      if (e.stage === "upload") {
        // A wrong-patient abort reason (or any failure TYPE) surfaces here.
        onUploadTerminal("upload stopped: " + e.error);
        showBanner("Upload stopped: " + e.error);
        Shell.logEvent({ kind: "error", msg: "upload stopped: " + e.error });
      }
      break;
    default:
      break;
  }
};

// --- error inspector flyout (TYPE histograms only) ------------------------
function renderErrorHist(hist) {
  const box = el("error-hist");
  box.innerHTML = "";
  const types = Object.keys(hist).sort();
  if (types.length === 0) {
    box.textContent = "No errors recorded for the current run.";
    return;
  }
  for (const t of types) {
    const row = document.createElement("div");
    row.className = "hist-row";
    const k = document.createElement("span");
    k.textContent = t;
    const v = document.createElement("span");
    v.textContent = String(hist[t]);
    row.appendChild(k);
    row.appendChild(v);
    box.appendChild(row);
  }
}

function toggleFlyout() {
  el("error-flyout").classList.toggle("show");
}

// --- calendar HUD: halo cells over the run's start/finish days ------------
// PHI-safe by construction: the only data plotted is the run's own ISO
// timestamps and a single count per active day — never patient values.
function buildCalendarFromRun(run) {
  CAL.histogram = {};
  if (!run || !run.started_at) {
    const now = new Date();
    CAL.year = now.getFullYear();
    CAL.month = now.getMonth();
    drawCalendar();
    return;
  }
  const started = run.started_at.slice(0, 10);
  const [y, m] = started.split("-").map((s) => parseInt(s, 10));
  CAL.year = y;
  CAL.month = m - 1;
  CAL.histogram[started] = { pending: 0, done: 1, errors: 0 };
  if (run.aborted_reason) {
    CAL.histogram[started] = { pending: 0, done: 0, errors: 1 };
  }
  if (run.finished_at) {
    const fin = run.finished_at.slice(0, 10);
    const cur = CAL.histogram[fin] || { pending: 0, done: 0, errors: 0 };
    cur.done += 1;
    CAL.histogram[fin] = cur;
  }
  drawCalendar();
}

function drawCalendar() {
  Shell.renderCalendar({
    gridEl: el("cal-grid"),
    titleEl: el("cal-title"),
    year: CAL.year,
    month: CAL.month,
    histogram: CAL.histogram,
  });
}

function navigateMonth(delta) {
  if (CAL.year === null) {
    const now = new Date();
    CAL.year = now.getFullYear();
    CAL.month = now.getMonth();
  }
  CAL.month += delta;
  while (CAL.month < 0) {
    CAL.month += 12;
    CAL.year -= 1;
  }
  while (CAL.month > 11) {
    CAL.month -= 12;
    CAL.year += 1;
  }
  drawCalendar();
}

// --- item-key command palette (Cmd/Ctrl+K): item KEYS only ----------------
// The palette is a read-only visibility surface: it lets the operator SEE which
// opaque keys still owe work. Selecting a key surfaces it; a run is driven by
// the start/stop buttons above (upload_start / upload_stop), not from here.
async function refreshItemKeys() {
  if (!hasApi()) {
    ITEM_KEYS = [];
    return;
  }
  const dbPath = el("db-path").value;
  if (!dbPath) {
    ITEM_KEYS = [];
    return;
  }
  try {
    const res = await window.pywebview.api.upload_item_keys(dbPath);
    ITEM_KEYS = res && res.ok ? res.item_keys : [];
  } catch (err) {
    ITEM_KEYS = [];
  }
}

function itemKeyCommands() {
  if (ITEM_KEYS.length === 0) {
    return [{ id: "none", label: "no pending item keys", hint: "ids", action: () => {} }];
  }
  return ITEM_KEYS.map((key) => ({
    id: key,
    label: key,
    hint: "id",
    // The palette only surfaces a key for visibility; the run is driven by the
    // start/stop buttons, so selecting a key is intentionally inert.
    action: () => {},
  }));
}

async function openItemKeyPalette() {
  await refreshItemKeys();
  // Rebuild the palette over the freshly fetched keys, then open it.
  PALETTE = Shell.initCommandPalette(itemKeyCommands());
  PALETTE.open();
}

// --- bootstrap ------------------------------------------------------------
async function populate() {
  if (!hasApi()) {
    el("no-api").classList.add("show");
    setStatus("offline");
    return;
  }
  try {
    const info = await window.pywebview.api.info();
    if (info && info.ok) {
      el("version").textContent = info.version;
    }
  } catch (err) {
    showBanner(err);
  }
  // Surface the shared-machine warning before any attach is possible.
  await loadSafetyNotice();
}

function init() {
  const load = el("load-btn");
  if (load) {
    load.addEventListener("click", onLoad);
  }
  const inspect = el("inspect-errors");
  if (inspect) {
    inspect.addEventListener("click", toggleFlyout);
  }
  const startBtn = el("start-upload-btn");
  if (startBtn) {
    startBtn.addEventListener("click", onStartUpload);
  }
  const stopBtn = el("stop-upload-btn");
  if (stopBtn) {
    stopBtn.addEventListener("click", onStopUpload);
  }
  const prev = el("cal-prev");
  if (prev) {
    prev.addEventListener("click", () => navigateMonth(-1));
  }
  const next = el("cal-next");
  if (next) {
    next.addEventListener("click", () => navigateMonth(1));
  }

  Shell.initLogStrip();
  PALETTE = Shell.initCommandPalette(itemKeyCommands());

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      if (PALETTE && PALETTE.isOpen()) {
        PALETTE.close();
      } else {
        openItemKeyPalette();
      }
    } else if (e.key === "l" || e.key === "L") {
      const tag = (document.activeElement && document.activeElement.tagName) || "";
      if (tag !== "INPUT" && tag !== "TEXTAREA" && !(PALETTE && PALETTE.isOpen())) {
        e.preventDefault();
        Shell.toggleLogDrawer();
      }
    }
  });

  populate();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
