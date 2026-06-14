/*
 * Anastomosis GUI dashboard — vanilla JS, no frameworks, no build step.
 *
 * Talks to the headless controller (anastomosis.gui.controller.GuiController)
 * over pywebview's bridge: every call is `window.pywebview.api.<method>(...)`,
 * returning a Promise of a JSON-safe dict. Progress arrives the other way,
 * pushed by the shell as `window.anastEvent(<event>)`.
 *
 * The visual layer + interaction patterns (gooey segment toggle, command
 * palette, log strip/drawer) are carried from the predecessor GUI via
 * window.AnastShell. The controller seam is untouched.
 *
 * PHI discipline mirrors the controller: this UI renders counts, stage names,
 * ids, and exception type names. It never receives — and so cannot show —
 * patient field values or rendered filenames.
 *
 * Guarded so opening index.html in a PLAIN browser (no pywebview) shows the
 * "launch via anast gui" notice instead of throwing.
 */
"use strict";

const RAIL = ["ingest", "reconstruct", "qa", "deliver"];
const Shell = window.AnastShell;

function hasApi() {
  return typeof window.pywebview !== "undefined" && !!window.pywebview.api;
}

function el(id) {
  return document.getElementById(id);
}

// --- the event dispatcher the shell (Python side) calls -------------------
window.anastEvent = function anastEvent(e) {
  if (!e || typeof e !== "object") {
    return;
  }
  switch (e.type) {
    case "stage":
      markStage(e.stage, e.state);
      Shell.logEvent({ kind: "info", msg: `stage ${e.stage}: ${e.state}` });
      break;
    case "progress":
      renderCounters(e);
      Shell.logEvent({ kind: "info", msg: `progress ${e.stage}: ${counterText(e)}` });
      break;
    case "done":
      Shell.logEvent({ kind: "ok", msg: `done: ${counterText(e)}` });
      finishRun();
      loadPatients();
      break;
    case "error":
      markStage(e.stage, "error");
      showBanner(e.error);
      Shell.logEvent({ kind: "error", msg: `error ${e.stage}: ${e.error}` });
      finishRun();
      break;
    default:
      break;
  }
};

function markStage(stage, state) {
  const card = el(`stage-${stage}`);
  if (card) {
    card.setAttribute("data-state", state);
  }
}

function counterText(e) {
  return Object.keys(e)
    .filter((k) => k !== "type" && k !== "stage" && k !== "state")
    .map((k) => `${k}=${e[k]}`)
    .join(" ");
}

function renderCounters(e) {
  const card = el(`stage-${e.stage}`);
  if (!card) {
    return;
  }
  const counters = card.querySelector(".counters");
  if (counters) {
    counters.textContent = counterText(e);
  }
}

function showBanner(message) {
  const banner = el("banner");
  if (banner) {
    banner.textContent = "Run failed: " + message;
    banner.classList.add("show");
  }
}

function hideBanner() {
  const banner = el("banner");
  if (banner) {
    banner.classList.remove("show");
  }
}

function setBusy(busy) {
  const btn = el("run-btn");
  if (btn) {
    btn.disabled = busy;
    btn.textContent = busy ? "running…" : "run pipeline";
  }
  const frame = document.querySelector(".progress-frame");
  if (frame) {
    frame.classList.toggle("is-running", busy);
  }
  setStatus(busy ? "running" : "ready");
}

function setStatus(text) {
  const t = el("status-text");
  if (t) {
    t.textContent = text;
  }
}

function resetRail() {
  for (const stage of RAIL) {
    const card = el(`stage-${stage}`);
    if (card) {
      card.removeAttribute("data-state");
      const counters = card.querySelector(".counters");
      if (counters) {
        counters.textContent = "";
      }
    }
  }
  const fill = el("progress-bar-fill");
  if (fill) {
    fill.style.width = "0%";
  }
}

function finishRun() {
  setBusy(false);
  const fill = el("progress-bar-fill");
  if (fill) {
    fill.style.width = "100%";
  }
}

// --- the Section-Selection Matrix (item 18b) ------------------------------
// info().packs[].sections is {key: {label, default}}; cache per pack name so
// switching packs repaints the matrix without another round-trip.
let SECTIONS_BY_PACK = {};

function gatherSections() {
  const sections = {};
  const boxes = el("section-matrix").querySelectorAll("input[type=checkbox]");
  for (const box of boxes) {
    sections[box.dataset.section] = box.checked;
  }
  return sections;
}

function renderSectionMatrix(packName) {
  const matrix = el("section-matrix");
  matrix.innerHTML = "";
  const sections = SECTIONS_BY_PACK[packName] || {};
  const keys = Object.keys(sections);
  if (keys.length === 0) {
    matrix.textContent = "This pack exposes no togglable sections.";
    matrix.classList.add("empty");
    return;
  }
  matrix.classList.remove("empty");
  for (const key of keys) {
    const flag = sections[key];
    const label = document.createElement("label");
    label.className = "toggle";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.section = key;
    input.checked = flag.default !== false;
    const track = document.createElement("span");
    track.className = "track";
    const text = document.createElement("span");
    text.textContent = flag.label || key;
    label.appendChild(input);
    label.appendChild(track);
    label.appendChild(text);
    matrix.appendChild(label);
  }
}

async function populateHeader() {
  if (!hasApi()) {
    el("no-api").classList.add("show");
    setBusy(true); // no controller to run against; keep the button inert
    setStatus("offline");
    return;
  }
  try {
    const info = await window.pywebview.api.info();
    if (info && info.ok) {
      el("version").textContent = info.version;
      const select = el("pack");
      select.innerHTML = "";
      SECTIONS_BY_PACK = {};
      for (const pack of info.packs) {
        if (!pack.available) {
          continue;
        }
        SECTIONS_BY_PACK[pack.name] = pack.sections || {};
        const opt = document.createElement("option");
        opt.value = pack.name;
        opt.textContent = pack.name;
        select.appendChild(opt);
      }
      renderSectionMatrix(select.value);
      populateSources(info.sources || []);
      setStatus("ready");
    }
  } catch (err) {
    showBanner(String(err));
  }
  checkFreshness();
}

// Source picker: "auto-detect" (empty value → null → pipeline sniffs the
// export) plus every registered adapter from info().sources.
function populateSources(sources) {
  const select = el("source");
  if (!select) {
    return;
  }
  select.innerHTML = "";
  const auto = document.createElement("option");
  auto.value = "";
  auto.textContent = "auto-detect";
  select.appendChild(auto);
  for (const src of sources) {
    const opt = document.createElement("option");
    opt.value = src.name;
    opt.textContent = src.name;
    select.appendChild(opt);
  }
}

// --- vendor-change detection toast (pack_freshness) -----------------------
async function checkFreshness() {
  if (!hasApi()) {
    return;
  }
  try {
    const res = await window.pywebview.api.pack_freshness();
    if (!res || !res.ok || !Array.isArray(res.stale) || res.stale.length === 0) {
      return;
    }
    const names = res.stale.map((s) => s.destination).join(", ");
    const advice = res.stale[0].advice;
    el("freshness-body").textContent =
      "Local selectors may be stale (>" +
      res.stale_after_days +
      " days vs verified evidence): " +
      names +
      ". Re-validate: " +
      advice;
    el("freshness-toast").classList.add("show");
  } catch (err) {
    // A freshness probe is advisory; never block the dashboard on it.
  }
}

function dismissFreshness() {
  el("freshness-toast").classList.remove("show");
}

async function onRun() {
  if (!hasApi()) {
    return;
  }
  hideBanner();
  resetRail();
  clearPatients();
  setBusy(true);
  Shell.logEvent({ kind: "info", msg: "run requested" });
  const qa = Shell.segmentValue("qa", "on") === "on";
  const sourceValue = el("source") ? el("source").value : "";
  const packDir = el("pack-dir") ? el("pack-dir").value.trim() : "";
  const payload = {
    export_dir: el("export-dir").value,
    out_dir: el("out-dir").value,
    pack: el("pack").value,
    // empty selection → auto-detect (the controller treats null as "sniff").
    source: sourceValue || null,
    sections: gatherSections(),
    qa: qa,
    archive: gatherDeliver("archive"),
    bundle: gatherDeliver("bundle"),
    ccda: gatherDeliver("ccda"),
    force: !!(el("force") && el("force").checked),
    pack_dirs: packDir ? [packDir] : [],
    trust_new: !!(el("trust-pack") && el("trust-pack").checked),
  };
  try {
    // Fire-and-forget on a worker thread; results stream back via anastEvent.
    const started = await window.pywebview.api.run_pipeline_async(
      payload.export_dir,
      payload.out_dir,
      payload.pack,
      payload.source,
      payload.sections,
      payload.qa,
      payload.archive,
      payload.bundle,
      payload.ccda,
      payload.force,
      payload.pack_dirs,
      payload.trust_new
    );
    if (started && started.ok === false) {
      showBanner(started.error);
      setBusy(false);
    }
  } catch (err) {
    showBanner(String(err));
    setBusy(false);
  }
}

function gatherDeliver(name) {
  const box = el(`deliver-${name}`);
  return !!(box && box.checked);
}

// --- per-patient detail (local display; never on an event) ----------------
// The `done` event carries counts only; the names/DOB/note-counts are fetched
// here via last_run_summary() and rendered with textContent (PHI shown locally,
// never logged). The strict CSP forbids inline anyway.
async function loadPatients() {
  if (!hasApi()) {
    return;
  }
  try {
    const res = await window.pywebview.api.last_run_summary();
    if (res && res.ok) {
      renderPatients(res.patients || []);
    }
  } catch (err) {
    // The summary is advisory; never block the run roll-up on it.
  }
}

function clearPatients() {
  const body = el("patients-body");
  if (body) {
    body.innerHTML = "";
  }
  const panel = el("patients-panel");
  if (panel) {
    panel.hidden = true;
  }
}

function renderPatients(patients) {
  const panel = el("patients-panel");
  const body = el("patients-body");
  if (!panel || !body) {
    return;
  }
  body.innerHTML = "";
  if (!patients.length) {
    panel.hidden = true;
    return;
  }
  const table = document.createElement("table");
  table.className = "patients-table";
  const head = document.createElement("tr");
  for (const heading of ["patient", "dob", "encounters", "notes"]) {
    const th = document.createElement("th");
    th.textContent = heading;
    head.appendChild(th);
  }
  table.appendChild(head);
  for (const p of patients) {
    const tr = document.createElement("tr");
    const cells = [
      p.display_name || "—",
      p.birth_date || "—",
      String(p.encounters),
      String(p.documents),
    ];
    for (const value of cells) {
      const td = document.createElement("td");
      td.textContent = value; // textContent: PHI rendered as text, never HTML
      tr.appendChild(td);
    }
    table.appendChild(tr);
  }
  body.appendChild(table);
  panel.hidden = false;
}

function init() {
  // wallpaper title text already neutral; wire the controls.
  const btn = el("run-btn");
  if (btn) {
    btn.addEventListener("click", onRun);
  }
  const pack = el("pack");
  if (pack) {
    pack.addEventListener("change", () => renderSectionMatrix(pack.value));
  }
  const dismiss = el("freshness-dismiss");
  if (dismiss) {
    dismiss.addEventListener("click", dismissFreshness);
  }

  Shell.initSegmentToggles(document);
  Shell.initLogStrip();

  // Command palette: PHI-free dashboard actions only.
  const palette = Shell.initCommandPalette([
    { id: "run", label: "Run pipeline", hint: "run", action: () => onRun() },
    { id: "qa-on", label: "Enable QA", hint: "toggle", action: () => setSegment("qa", "on") },
    { id: "qa-off", label: "Disable QA", hint: "toggle", action: () => setSegment("qa", "off") },
    { id: "archive", label: "Toggle archive output", hint: "deliver", action: () => toggleDeliver("archive") },
    { id: "log", label: "Toggle activity log", hint: "view", action: () => Shell.toggleLogDrawer() },
    { id: "wizard", label: "Open migration wizard", hint: "go", action: () => { window.location.href = "wizard.html"; } },
    { id: "console", label: "Open upload console", hint: "go", action: () => { window.location.href = "console.html"; } },
    { id: "packgen", label: "Open pack from samples", hint: "go", action: () => { window.location.href = "packgen.html"; } },
  ]);
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      palette.toggle();
    } else if (e.key === "l" || e.key === "L") {
      const tag = (document.activeElement && document.activeElement.tagName) || "";
      if (tag !== "INPUT" && tag !== "TEXTAREA" && !palette.isOpen()) {
        e.preventDefault();
        Shell.toggleLogDrawer();
      }
    }
  });

  populateHeader();
}

function setSegment(name, value) {
  const host = document.querySelector(`.segment-toggle[data-name="${name}"]`);
  if (!host) {
    return;
  }
  const btn = host.querySelector(`.segment-option[data-value="${value}"]`);
  if (btn) {
    btn.click();
  }
}

function toggleDeliver(name) {
  const box = el(`deliver-${name}`);
  if (box) {
    box.checked = !box.checked;
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
