/*
 * Anastomosis GUI — Teach, mode 1: teach a document layout from samples.
 *
 * Owns the "pack_init" flow and registers the Teach view itself (mode 2,
 * export formats, lives in source.js and registers only its own flow).
 *
 * Two steps, gated the way the CLI gates them:
 *   1. "Look at the samples" calls pack_init_async(confirmed=false) — the
 *      controller refuses to write and stashes the summary, so the operator
 *      sees exactly what they are confirming;
 *   2. the confirmation enables "Write the draft layout", which calls
 *      pack_init_async(confirmed=true) to emit the draft.
 * Both are fire-and-forget: the call returns {started:true} and the real result
 * arrives via the shell's dispatcher → last_pack_result().
 *
 * PHI discipline: the summary carries static template text (recurring across
 * distinct samples) and counts only; sample paths are never echoed.
 */
"use strict";

(function () {
  const Shell = window.AnastShell;
  const el = (id) => document.getElementById(id);

  function hasApi() {
    return Shell.hasApi();
  }

  function setStep(text) {
    el("layout-step").textContent = text;
  }

  function renderProposal(res) {
    el("layout-summary").textContent = (res.summary || []).join("\n");
    el("layout-caveat").textContent = res.caveat
      ? `About the samples: ${res.caveat}`
      : "";
    el("layout-proposal").hidden = false;
    // Consent is per-analysis, never sticky: a fresh look re-arms the gate.
    el("layout-confirm").checked = false;
    el("layout-write").disabled = true;
    setStep("Step 2 of 2 — review and confirm.");
  }

  function renderWritten(res) {
    el("layout-result-path").textContent = `The draft layout was written to ${res.pack_dir}`;
    el("layout-result-md").textContent = res.draft_md || "";
    el("layout-result").hidden = false;
    setStep("Done. Review the draft against an original sample before using it.");
  }

  // ConfirmationRequired is the EXPECTED outcome of step 1 (it carries the
  // summary to confirm); ok is the written draft; anything else is a failure.
  function route(res) {
    if (res && res.ok) {
      renderWritten(res);
    } else if (res && res.error === "ConfirmationRequired") {
      renderProposal(res);
    } else {
      Shell.showBanner(
        `The samples could not be turned into a layout: ${res ? res.error : "no answer from the app"}`
      );
    }
  }

  async function fetchResult() {
    if (!hasApi()) return;
    try {
      route(await window.pywebview.api.last_pack_result());
    } catch (err) {
      Shell.showBanner(String(err));
    }
  }

  function onEvent(event) {
    if (event.type === "stage" && event.stage === "packgen" && event.state === "done") {
      fetchResult();
    } else if (event.type === "done") {
      fetchResult();
    } else if (event.type === "error") {
      Shell.showBanner(`The samples could not be turned into a layout: ${event.error}`);
    }
  }

  function values() {
    return {
      samples: el("layout-samples").value,
      name: el("layout-name").value,
      display: el("layout-display").value || null,
    };
  }

  async function onAnalyze() {
    if (!hasApi()) return;
    Shell.hideBanner();
    el("layout-result").hidden = true;
    const v = values();
    setStep("Step 1 of 2 — looking at the samples…");
    try {
      const started = await window.pywebview.api.pack_init_async(v.samples, v.name, v.display, false);
      if (started && started.ok === false) {
        Shell.showBanner(Shell.refusalText(started.error, "The samples could not be read"));
        setStep("Step 1 of 2 — look at the samples.");
      }
    } catch (err) {
      Shell.showBanner(String(err));
    }
  }

  async function onWrite() {
    if (!hasApi() || !el("layout-confirm").checked) return;
    Shell.hideBanner();
    const v = values();
    setStep("Step 2 of 2 — writing the draft…");
    try {
      const started = await window.pywebview.api.pack_init_async(v.samples, v.name, v.display, true);
      if (started && started.ok === false) {
        Shell.showBanner(Shell.refusalText(started.error, "The draft layout could not be written"));
        setStep("Step 2 of 2 — review and confirm.");
      }
    } catch (err) {
      Shell.showBanner(String(err));
    }
  }

  function init() {
    el("layout-analyze").addEventListener("click", onAnalyze);
    el("layout-write").addEventListener("click", onWrite);
    el("layout-confirm").addEventListener("change", () => {
      el("layout-write").disabled = !el("layout-confirm").checked;
    });
    Shell.onReady((live) => {
      el("layout-analyze").disabled = !live;
    });
  }

  // The Teach VIEW is registered here (one workspace, two modes); source.js
  // registers only the second mode's flow.
  Shell.registerView({
    name: "teach",
    title: "Teach",
    flow: "pack_init",
    onEvent,
  });
  init();
})();
