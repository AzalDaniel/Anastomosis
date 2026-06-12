/*
 * Anastomosis Upload Console — vanilla JS, no frameworks, no build step.
 *
 * READ-ONLY surface over an existing tracking ledger. Talks to the headless
 * controller:
 *   - upload_status(db)          → grouped state counters + run row + histograms
 *   - upload_manifest_preview(d) → count of renderable PDFs (no names)
 *   - upload_item_keys(db)       → pending item KEYS for the Cmd/Ctrl+K palette
 *
 * The visual layer is carried from the predecessor (asymmetric counter grid,
 * calendar HUD with halo cells, command palette, log strip/drawer) via
 * window.AnastShell. The console mirrors the original app-shell most closely:
 * the counter grid is wired to OUR ledger state groups; the calendar plots
 * the run's start/finish days with the original halo treatment.
 *
 * No live driving here — starting/pausing real uploads is a later milestone and
 * is labeled as such in the UI. There are NO buttons that pretend to upload.
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
// Selecting a key is a no-op for now — live driving is a later milestone. The
// palette only lets the operator SEE which opaque keys still owe work.
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
    // Read-only: surfacing a key is the whole behaviour. Driving is deferred.
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
