/*
 * Anastomosis Upload Console — vanilla JS, no frameworks, no build step.
 *
 * READ-ONLY surface over an existing tracking ledger. Talks to the headless
 * controller:
 *   - upload_status(db)          → grouped state counters + run row + histograms
 *   - upload_manifest_preview(d) → count of renderable PDFs (no names)
 *   - upload_item_keys(db)       → pending item KEYS for the Cmd+K palette
 *
 * No live driving here — starting/pausing real uploads is a later milestone and
 * is labeled as such in the UI. There are NO buttons that pretend to upload.
 *
 * PHI discipline: every value rendered is a count, a state name, a run/
 * destination id, an ISO timestamp, an exception TYPE name, or an opaque item
 * key (encounter id + hash). Never a patient name, never a file path.
 */
"use strict";

function hasApi() {
  return typeof window.pywebview !== "undefined" && !!window.pywebview.api;
}

function el(id) {
  return document.getElementById(id);
}

function showBanner(message) {
  const banner = el("banner");
  if (banner) {
    banner.textContent = String(message);
    banner.classList.add("show");
  }
}

let CURRENT_RUN = null; // the latest run row (for the error histogram + calendar)

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
      return;
    }
    renderStatus(status);
    renderErrorHist(status.error_type_histogram || {});
    renderCalendar(status.run);
    CURRENT_RUN = status.run;
    el("run-panel").hidden = false;
    el("calendar-panel").hidden = false;
  } catch (err) {
    showBanner(err);
  }

  const outDir = el("out-dir").value;
  if (outDir) {
    try {
      const preview = await window.pywebview.api.upload_manifest_preview(outDir);
      if (preview && preview.ok) {
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
  // Run row.
  const run = status.run;
  el("run-info").textContent = run
    ? "run " +
      run.run_id +
      " · " +
      run.destination +
      " · started " +
      run.started_at +
      (run.finished_at ? " · finished " + run.finished_at : " · in progress") +
      (run.aborted_reason ? " · aborted (" + run.aborted_reason + ")" : "")
    : "No runs recorded in this ledger.";

  // Grouped glass cards (pending / active / terminal).
  const groups = status.groups || {};
  const cards = el("group-cards");
  cards.innerHTML = "";
  for (const group of ["pending", "active", "terminal"]) {
    const card = document.createElement("div");
    card.className = "count-card";
    card.setAttribute("data-group", group);
    const g = document.createElement("div");
    g.className = "group";
    g.textContent = group;
    const n = document.createElement("div");
    n.className = "n";
    n.textContent = String(groups[group] || 0);
    card.appendChild(g);
    card.appendChild(n);
    cards.appendChild(card);
  }

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

// --- calendar HUD with halo dots ------------------------------------------
function renderCalendar(run) {
  const cal = el("calendar");
  cal.innerHTML = "";
  if (!run || !run.started_at) {
    el("calendar-month").textContent = "No runs to plot.";
    return;
  }
  // started_at is an ISO timestamp; take its month and dot the active days.
  const started = run.started_at.slice(0, 10);
  const [year, month] = started.split("-").map((s) => parseInt(s, 10));
  el("calendar-month").textContent = year + "-" + String(month).padStart(2, "0");
  const activeDays = new Set();
  activeDays.add(parseInt(started.slice(8, 10), 10));
  if (run.finished_at) {
    const fin = run.finished_at.slice(0, 10);
    if (fin.slice(0, 7) === started.slice(0, 7)) {
      activeDays.add(parseInt(fin.slice(8, 10), 10));
    }
  }
  const days = new Date(year, month, 0).getDate();
  for (let d = 1; d <= days; d++) {
    const cell = document.createElement("div");
    cell.className = "cal-cell";
    cell.setAttribute("data-dot", String(activeDays.has(d)));
    cell.textContent = String(d);
    cal.appendChild(cell);
  }
}

// --- command palette STUB (Cmd+K): item KEYS only -------------------------
let ITEM_KEYS = [];

async function openPalette() {
  if (!hasApi()) {
    return;
  }
  const dbPath = el("db-path").value;
  if (!dbPath) {
    return;
  }
  try {
    const res = await window.pywebview.api.upload_item_keys(dbPath);
    ITEM_KEYS = res && res.ok ? res.item_keys : [];
  } catch (err) {
    ITEM_KEYS = [];
  }
  renderPaletteList("");
  el("palette-backdrop").classList.add("show");
  el("palette-filter").focus();
}

function closePalette() {
  el("palette-backdrop").classList.remove("show");
}

function renderPaletteList(filter) {
  const list = el("palette-list");
  list.innerHTML = "";
  const needle = filter.toLowerCase();
  const matches = ITEM_KEYS.filter((k) => k.toLowerCase().includes(needle));
  if (matches.length === 0) {
    list.textContent = "No matching item keys.";
    return;
  }
  for (const key of matches) {
    const row = document.createElement("div");
    row.className = "palette-item";
    row.textContent = key;
    list.appendChild(row);
  }
}

function onKeydown(e) {
  if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
    e.preventDefault();
    openPalette();
  } else if (e.key === "Escape") {
    closePalette();
  }
}

// --- bootstrap ------------------------------------------------------------
async function populate() {
  if (!hasApi()) {
    el("no-api").classList.add("show");
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
  const filter = el("palette-filter");
  if (filter) {
    filter.addEventListener("input", () => renderPaletteList(filter.value));
  }
  const backdrop = el("palette-backdrop");
  if (backdrop) {
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) {
        closePalette();
      }
    });
  }
  document.addEventListener("keydown", onKeydown);
  populate();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
