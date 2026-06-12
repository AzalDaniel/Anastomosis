/*
 * Anastomosis Pack-from-samples wizard — vanilla JS, no frameworks, no build.
 *
 * Talks to the headless controller's pack_init(), which wraps packgen
 * analyze+emit. The same-patient guard from the CLI is ported as a REQUIRED
 * checkbox:
 *   - "Analyze samples" calls pack_init(confirmed=false): the controller
 *     refuses to emit but returns the PHI-safe summary + the caveat, so the
 *     operator sees exactly what they are confirming.
 *   - the checkbox enables "Write draft pack", which calls pack_init(
 *     confirmed=true) to emit and return the draft path + DRAFT.md text.
 *
 * PHI discipline: summary lines carry only static template text (recurring
 * across distinct samples) and counts; sample paths are never echoed. The
 * single-sample text suppression is inherited from the controller/summary.
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

function hideBanner() {
  const banner = el("banner");
  if (banner) {
    banner.classList.remove("show");
  }
}

function renderSummary(res) {
  el("summary").textContent = (res.summary || []).join("\n");
  if (res.caveat) {
    el("caveat").textContent = "Same-patient caveat: " + res.caveat;
  }
  el("summary-panel").hidden = false;
  // Reset the confirmation for each fresh analysis.
  el("confirm-distinct").checked = false;
  el("emit-btn").disabled = true;
}

// Step 1: analyze (confirmed=false → summary + caveat, no emit).
async function onAnalyze() {
  if (!hasApi()) {
    return;
  }
  hideBanner();
  el("draft-panel").hidden = true;
  const samplesDir = el("samples-dir").value;
  const name = el("pack-name").value;
  const display = el("pack-display").value || null;
  try {
    const res = await window.pywebview.api.pack_init(samplesDir, name, display, false);
    // ConfirmationRequired is the EXPECTED outcome of the analyze step: it
    // carries the summary so the operator can confirm. Other errors are real.
    if (res && res.error === "ConfirmationRequired") {
      renderSummary(res);
    } else if (res && res.ok) {
      // Shouldn't happen with confirmed=false, but handle defensively.
      renderSummary(res);
    } else {
      showBanner("Analysis failed: " + (res ? res.error : "no response"));
    }
  } catch (err) {
    showBanner(err);
  }
}

// Step 2: emit (confirmed=true → write draft, return path + DRAFT.md).
async function onEmit() {
  if (!hasApi() || !el("confirm-distinct").checked) {
    return;
  }
  hideBanner();
  const samplesDir = el("samples-dir").value;
  const name = el("pack-name").value;
  const display = el("pack-display").value || null;
  try {
    const res = await window.pywebview.api.pack_init(samplesDir, name, display, true);
    if (res && res.ok) {
      el("draft-path").textContent = "Wrote draft pack to " + res.pack_dir;
      el("draft-md").textContent = res.draft_md || "";
      el("draft-panel").hidden = false;
    } else {
      showBanner("Emit failed: " + (res ? res.error : "no response"));
    }
  } catch (err) {
    showBanner(err);
  }
}

function onConfirmToggle() {
  el("emit-btn").disabled = !el("confirm-distinct").checked;
}

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
  const analyze = el("analyze-btn");
  if (analyze) {
    analyze.addEventListener("click", onAnalyze);
  }
  const emit = el("emit-btn");
  if (emit) {
    emit.addEventListener("click", onEmit);
  }
  const confirm = el("confirm-distinct");
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
