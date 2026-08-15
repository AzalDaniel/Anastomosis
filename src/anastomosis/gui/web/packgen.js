/*
 * Anastomosis Pack-from-samples wizard — vanilla JS, no frameworks, no build.
 *
 * Talks to the headless controller's pack_init_async(), which wraps packgen
 * analyze+emit on a daemon thread (the GUI stays responsive). The result is
 * fetched after the `packgen` `done`/`error` event via last_pack_result(). The
 * same-patient guard from the CLI is ported as a REQUIRED checkbox (the
 * controller's confirmed_distinct_patients argument):
 *   - "Analyze samples" calls pack_init_async(confirmed=false): the controller
 *     refuses to emit but stashes the PHI-safe summary + the caveat, so the
 *     operator sees exactly what they are confirming.
 *   - the checkbox enables "Write draft pack", which calls pack_init_async(
 *     confirmed=true) to emit and stash the draft path + DRAFT.md text.
 *
 * Both steps are fire-and-forget: the call returns {started:true} and the real
 * result arrives via window.anastEvent → last_pack_result(). A
 * ConfirmationRequired result routes to renderSummary; an ok result routes to
 * the draft-render branch — exactly as the old await-return path did.
 *
 * The visual layer (glass cards, the liquid confirm toggle) is carried from the
 * predecessor. The controller seam is untouched.
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

function renderDraft(res) {
  el("draft-path").textContent = "Wrote draft pack to " + res.pack_dir;
  el("draft-md").textContent = res.draft_md || "";
  el("draft-panel").hidden = false;
  setStatus("draft written");
}

// Route a fetched pack_init result to the right panel — shared by the analyze
// and emit steps, since the async call returns {started:true} and the real
// result lands via the event handler. ConfirmationRequired is the EXPECTED
// outcome of the analyze step (it carries the summary to confirm); ok is the
// written draft; anything else is a real failure.
function renderPackResult(res) {
  if (res && res.ok) {
    renderDraft(res);
  } else if (res && res.error === "ConfirmationRequired") {
    renderSummary(res);
    setStatus("review and confirm");
  } else {
    showBanner("Pack init failed: " + (res ? res.error : "no response"));
    setStatus("failed");
  }
}

// The event dispatcher the shell (Python side) calls during an async pack-init.
// On a packgen `done` (or a generic `done`) we fetch the stashed result and
// route it; an `error` shows the banner. Other stages just update the status.
// Flow guard (P2-5): the pack-from-samples wizard owns the "pack_init" flow. Every
// event carries a `flow`; we early-return on any other flow so a run from another
// page can't drive this wizard's terminal handlers.
window.anastEvent = function anastEvent(e) {
  if (!e || typeof e !== "object") {
    return;
  }
  if (e.flow !== "pack_init") {
    return;
  }
  switch (e.type) {
    case "stage":
      if (e.stage === "packgen" && e.state === "done") {
        fetchPackResult();
      } else {
        setStatus("stage " + e.stage + ": " + e.state);
      }
      break;
    case "done":
      fetchPackResult();
      break;
    case "error":
      showBanner("Pack init failed: " + e.error);
      setStatus("failed");
      break;
    default:
      break;
  }
};

async function fetchPackResult() {
  if (!hasApi()) {
    return;
  }
  try {
    const res = await window.pywebview.api.last_pack_result();
    renderPackResult(res);
  } catch (err) {
    showBanner(err);
  }
}

// Step 1: analyze (confirmed_distinct_patients=false → summary + caveat, no
// emit). Fire-and-forget: the result arrives via the event → last_pack_result.
async function onAnalyze() {
  if (!hasApi()) {
    return;
  }
  hideBanner();
  el("draft-panel").hidden = true;
  const samplesDir = el("samples-dir").value;
  const name = el("pack-name").value;
  const display = el("pack-display").value || null;
  setStatus("analyzing…");
  try {
    const started = await window.pywebview.api.pack_init_async(samplesDir, name, display, false);
    if (started && started.ok === false) {
      showBanner("Analysis failed: " + started.error);
      setStatus("analysis failed");
    }
  } catch (err) {
    showBanner(err);
  }
}

// Step 2: emit (confirmed_distinct_patients=true → write draft). Fire-and-forget:
// the draft path + DRAFT.md arrive via the event → last_pack_result.
async function onEmit() {
  if (!hasApi() || !el("confirm-distinct").checked) {
    return;
  }
  hideBanner();
  const samplesDir = el("samples-dir").value;
  const name = el("pack-name").value;
  const display = el("pack-display").value || null;
  setStatus("writing draft…");
  try {
    const started = await window.pywebview.api.pack_init_async(samplesDir, name, display, true);
    if (started && started.ok === false) {
      showBanner("Emit failed: " + started.error);
      setStatus("emit failed");
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
