/*
 * Anastomosis Learn-a-source wizard — vanilla JS, no frameworks, no build.
 *
 * Talks to the headless controller's source_init(), which wraps the
 * sourcelearn analyze -> build -> round_trip -> save flow. The two-step shape
 * mirrors the pack-from-samples wizard:
 *   - "Analyze example" calls source_init(confirmed=false): the controller
 *     refuses to write but returns the PHI-safe proposed mapping (grouping +
 *     per-column suggestions + analysis), so the operator sees exactly what
 *     they are confirming.
 *   - the "I reviewed this mapping" checkbox enables "Save mapping", which
 *     calls source_init(confirmed=true) to build the mapping, prove it drops no
 *     column (round-trip), save it owner-only, and return the path + MAPPING.md.
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

function formValues() {
  return {
    example: el("example-path").value,
    name: el("source-name").value,
    display: el("source-display").value || null,
  };
}

// Step 1: analyze (confirmed=false → proposed mapping, no write).
async function onAnalyze() {
  if (!hasApi()) {
    return;
  }
  hideBanner();
  el("result-panel").hidden = true;
  const { example, name, display } = formValues();
  setStatus("analyzing…");
  try {
    const res = await window.pywebview.api.source_init(example, name, display, false);
    // ConfirmationRequired is the EXPECTED outcome of the analyze step.
    if (res && res.error === "ConfirmationRequired") {
      renderProposal(res);
      setStatus("review and confirm");
    } else if (res && res.ok) {
      renderProposal(res);
    } else {
      showBanner("Analysis failed: " + (res ? res.error : "no response"));
      setStatus("analysis failed");
    }
  } catch (err) {
    showBanner(err);
  }
}

// Step 2: save (confirmed=true → round-trip + write, return path + MAPPING.md).
async function onSave() {
  if (!hasApi() || !el("confirm-review").checked) {
    return;
  }
  hideBanner();
  const { example, name, display } = formValues();
  setStatus("verifying and saving…");
  try {
    const res = await window.pywebview.api.source_init(example, name, display, true);
    if (res && res.ok) {
      el("result-path").textContent = "Saved learned source to " + res.mapping_dir;
      el("mapping-md").textContent = res.mapping_md || "";
      el("result-panel").hidden = false;
      setStatus("saved");
    } else if (res && res.error === "WouldDropColumns") {
      showBanner("Refusing to save — these columns would be dropped: " + (res.dropped || []).join(", "));
      setStatus("would drop columns");
    } else if (res && res.error === "MappingLoadFailed") {
      showBanner("Cannot load with this mapping — fix a column transform: " + (res.detail || ""));
      setStatus("mapping load failed");
    } else {
      showBanner("Save failed: " + (res ? res.error : "no response"));
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
