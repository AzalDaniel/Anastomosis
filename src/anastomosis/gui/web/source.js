/*
 * Anastomosis Learn-a-source wizard — vanilla JS, no frameworks, no build.
 *
 * Talks to the headless controller's source_init_async(), which wraps the
 * shared sourcelearn analyze -> build -> round_trip -> save flow on a daemon
 * thread (the GUI stays responsive). Both steps are fire-and-forget: the call
 * returns {started:true} and the real result arrives via window.anastEvent ->
 * last_source_result(). The two-step shape mirrors the pack-from-samples wizard:
 *   - "Analyze example" calls source_init_async(confirmed=false): the controller
 *     refuses to write but stashes the PHI-safe proposed mapping (grouping +
 *     per-column suggestions + analysis), so the operator sees exactly what they
 *     are confirming (ConfirmationRequired routes to renderProposal).
 *   - the "I reviewed this mapping" checkbox enables "Save mapping", which calls
 *     source_init_async(confirmed=true) to build the mapping, prove it drops no
 *     column (round-trip), save it owner-only, and stash the path + MAPPING.md.
 *
 * PHI discipline: the proposed mapping carries column NAMES, inferred type
 * labels, counts, and digit/letter-masked shapes only — never a cell value.
 */
"use strict";

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

function hideBanner() {
  const banner = el("banner");
  if (banner) {
    banner.classList.remove("show");
  }
}

function renderProposal(res) {
  el("grouping").textContent =
    "format " +
    res.format +
    " · " +
    res.columns +
    " columns · patient key " +
    JSON.stringify(res.patient_key) +
    " · encounter key " +
    JSON.stringify(res.encounter_key) +
    " · row scope " +
    res.row_scope +
    " · " +
    res.mapped +
    " mapped";

  // Build the suggestions table with the DOM API + textContent (never innerHTML)
  // so a column name can never be interpreted as markup.
  const table = el("suggestions");
  table.textContent = "";
  const header = ["source column", "canonical field", "transform", "confidence"];
  const head = document.createElement("div");
  head.className = "suggestion-row suggestion-head";
  header.forEach((label) => {
    const cell = document.createElement("span");
    cell.textContent = label;
    head.appendChild(cell);
  });
  table.appendChild(head);
  (res.suggestions || []).forEach((s) => {
    const row = document.createElement("div");
    row.className = "suggestion-row";
    const cells = [
      s.source,
      s.target || "(unmapped → extensions)",
      s.transform,
      Math.round((s.confidence || 0) * 100) + "%",
    ];
    cells.forEach((value) => {
      const cell = document.createElement("span");
      cell.textContent = String(value);
      row.appendChild(cell);
    });
    table.appendChild(row);
  });

  el("summary").textContent = (res.summary || []).join("\n");
  el("proposal-panel").hidden = false;
  // Reset the review confirmation for each fresh analysis.
  el("confirm-review").checked = false;
  el("save-btn").disabled = true;
}

function renderSaved(res) {
  el("result-path").textContent = "Saved learned source to " + res.mapping_dir;
  el("mapping-md").textContent = res.mapping_md || "";
  el("result-panel").hidden = false;
  setStatus("saved");
}

// Route a fetched source_init result — shared by the analyze and save steps,
// since the async call returns {started:true} and the real result lands via the
// event handler. ConfirmationRequired is the EXPECTED analyze outcome (it
// carries the proposal); ok is the saved mapping; WouldDropColumns /
// MappingLoadFailed carry their own actionable detail; anything else is a plain
// failure.
function renderSourceResult(res) {
  if (res && res.ok) {
    renderSaved(res);
  } else if (res && res.error === "ConfirmationRequired") {
    renderProposal(res);
    setStatus("review and confirm");
  } else if (res && res.error === "WouldDropColumns") {
    showBanner("Refusing to save — these columns would be dropped: " + (res.dropped || []).join(", "));
    setStatus("would drop columns");
  } else if (res && res.error === "MappingLoadFailed") {
    showBanner("Cannot load with this mapping — fix a column transform: " + (res.detail || ""));
    setStatus("mapping load failed");
  } else {
    showBanner("Source init failed: " + (res ? res.error : "no response"));
    setStatus("failed");
  }
}

async function fetchSourceResult() {
  if (!hasApi()) {
    return;
  }
  try {
    const res = await window.pywebview.api.last_source_result();
    renderSourceResult(res);
  } catch (err) {
    showBanner(err);
  }
}

// The event dispatcher the shell (Python side) calls during an async run. On a
// `source` done OR a `source` error we fetch the stashed result and route it
// (the result carries the outcome-specific detail the banner needs); other
// stages just update the status.
// Flow guard: the learn-a-source wizard owns the "source_init" flow. Every
// event carries a `flow`; we early-return on any other flow so a run from another
// page can't drive this wizard's terminal handlers.
window.anastEvent = function anastEvent(e) {
  if (!e || typeof e !== "object") {
    return;
  }
  if (e.flow !== "source_init") {
    return;
  }
  switch (e.type) {
    case "stage":
      if (e.stage === "source" && e.state === "done") {
        fetchSourceResult();
      } else {
        setStatus("stage " + e.stage + ": " + e.state);
      }
      break;
    case "done":
      fetchSourceResult();
      break;
    case "error":
      fetchSourceResult();
      break;
    default:
      break;
  }
};

function formValues() {
  return {
    example: el("example-path").value,
    name: el("source-name").value,
    display: el("source-display").value || null,
  };
}

// Step 1: analyze (confirmed=false → proposed mapping, no write). Fire-and-forget:
// the proposal arrives via the event → last_source_result.
async function onAnalyze() {
  if (!hasApi()) {
    return;
  }
  hideBanner();
  el("result-panel").hidden = true;
  const { example, name, display } = formValues();
  setStatus("analyzing…");
  try {
    const started = await window.pywebview.api.source_init_async(example, name, display, false);
    if (started && started.ok === false) {
      showBanner("Analysis failed: " + started.error);
      setStatus("analysis failed");
    }
  } catch (err) {
    showBanner(err);
  }
}

// Step 2: save (confirmed=true → round-trip + write). Fire-and-forget: the path +
// MAPPING.md (or a drop/load refusal) arrive via the event → last_source_result.
async function onSave() {
  if (!hasApi() || !el("confirm-review").checked) {
    return;
  }
  hideBanner();
  const { example, name, display } = formValues();
  setStatus("verifying and saving…");
  try {
    const started = await window.pywebview.api.source_init_async(example, name, display, true);
    if (started && started.ok === false) {
      showBanner("Save failed: " + started.error);
      setStatus("save failed");
    }
  } catch (err) {
    showBanner(err);
  }
}

function onConfirmToggle() {
  el("save-btn").disabled = !el("confirm-review").checked;
}

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
  const analyze = el("analyze-btn");
  if (analyze) {
    analyze.addEventListener("click", onAnalyze);
  }
  const save = el("save-btn");
  if (save) {
    save.addEventListener("click", onSave);
  }
  const confirm = el("confirm-review");
  if (confirm) {
    confirm.addEventListener("change", onConfirmToggle);
  }
  populate();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
