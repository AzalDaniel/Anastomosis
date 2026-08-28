/*
 * Anastomosis GUI — Teach, mode 2: teach an export format from one example.
 *
 * Owns the "source_init" flow; the Teach view itself is registered by
 * packgen.js (one workspace, two modes, the same two-step shape).
 *
 *   1. "Look at the example" calls source_init_async(confirmed=false) — the
 *      controller refuses to write and stashes the proposed match-up;
 *   2. the confirmation enables "Save this format", which calls
 *      source_init_async(confirmed=true): it proves no column would be lost,
 *      then stores the format for this user account only.
 *
 * PHI discipline: the proposal carries column NAMES, inferred type labels,
 * counts and masked shapes only — never a cell value.
 */
"use strict";

(function () {
  const Shell = window.AnastShell;
  const el = (id) => document.getElementById(id);

  function hasApi() {
    return Shell.hasApi();
  }

  function setStep(text) {
    Shell.setStatus(el("format-step"), text);
  }

  function keyNames(key) {
    const names = Array.isArray(key) ? key : key ? [key] : [];
    return names.length ? names.join(" and ") : "nothing yet";
  }

  function renderProposal(res) {
    // Prose, not a key=value dump: what the file is, and how it is grouped.
    el("format-grouping").textContent =
      `${String(res.format).toUpperCase()} file · ${res.columns} columns · ` +
      `patients identified by ${keyNames(res.patient_key)} · ` +
      `one row per ${res.row_scope}.`;

    // Built with the DOM API and textContent (never innerHTML) so a column name
    // can never be interpreted as markup.
    const table = el("format-mapping");
    table.textContent = "";
    const head = document.createElement("div");
    head.className = "mapping-row mapping-head";
    for (const label of ["Column in your file", "Goes to", "How it is read", "Confidence"]) {
      const cell = document.createElement("span");
      cell.textContent = label;
      head.appendChild(cell);
    }
    table.appendChild(head);
    for (const s of res.suggestions || []) {
      const row = document.createElement("div");
      row.className = "mapping-row";
      const cells = [
        s.source,
        s.target || "(kept, unmatched — nothing is dropped)",
        s.transform,
        `${Math.round((s.confidence || 0) * 100)}%`,
      ];
      for (const value of cells) {
        const cell = document.createElement("span");
        cell.textContent = String(value);
        row.appendChild(cell);
      }
      table.appendChild(row);
    }

    el("format-summary").textContent = (res.summary || []).join("\n");
    el("format-proposal").hidden = false;
    // Consent is per-analysis, never sticky.
    el("format-confirm").checked = false;
    el("format-save").disabled = true;
    setStep("Step 2 of 2 — review and confirm.");
  }

  function renderSaved(res) {
    el("format-result-path").textContent = `The format was saved to ${res.mapping_dir}`;
    el("format-result-md").textContent = res.mapping_md || "";
    el("format-result").hidden = false;
    setStep("Done. This export format is now available when you rebuild charts.");
  }

  // ConfirmationRequired is the EXPECTED outcome of step 1. The two refusals
  // keep their loud semantics — in plain language, always saying what to do.
  function route(res) {
    if (res && res.ok) {
      renderSaved(res);
    } else if (res && res.error === "ConfirmationRequired") {
      renderProposal(res);
    } else if (res && res.error === "WouldDropColumns") {
      Shell.showBanner(
        `Cannot save yet — these columns would be lost: ${(res.dropped || []).join(", ")}. ` +
          "Every column must have a home before the format is saved."
      );
      setStep("Step 2 of 2 — review and confirm.");
    } else if (res && res.error === "MappingLoadFailed") {
      Shell.showBanner(
        `This file cannot be read with the proposed match-up yet: ${res.detail || ""}. ` +
          "Change how one of the columns is read, then look at the example again."
      );
      setStep("Step 2 of 2 — review and confirm.");
    } else {
      Shell.showBanner(
        `The example could not be turned into a format: ${res ? res.error : "no answer from the app"}`
      );
    }
  }

  async function fetchResult() {
    if (!hasApi()) return;
    try {
      route(await window.pywebview.api.last_source_result());
    } catch (err) {
      setAnalyzing(false);
      Shell.showBanner(String(err));
    }
  }

  // Both the terminal stage AND an error fetch the stashed result: the result
  // carries the outcome-specific detail (which columns, which transform) that a
  // bare error string does not.
  //: Held from the click until the run's terminal event.
  //:
  //: NOT `Shell.guardButton`: that releases when its `work` resolves, and
  //: `source_init_async` resolves as soon as the WORKER STARTS. Three rapid
  //: clicks still fired three analyses through it — measured, not assumed.
  //: The only on-screen feedback here is the step line, which is not a live
  //: region, so a screen-reader operator gets nothing from a click and will
  //: reasonably click again.
  let analyzeLabel = "";
  function setAnalyzing(busy) {
    const button = el("format-analyze");
    if (!button) return;
    // Remember the button's OWN label rather than re-typing it here, so the
    // markup stays the single place the wording lives.
    if (!analyzeLabel) analyzeLabel = button.textContent;
    button.disabled = busy;
    button.textContent = busy ? "Looking…" : analyzeLabel;
  }

  function onEvent(event) {
    if (event.type === "done" || event.type === "error" || event.state === "done") {
      setAnalyzing(false);
    }
    if (event.type === "stage" && event.stage === "source" && event.state === "done") {
      fetchResult();
    } else if (event.type === "done" || event.type === "error") {
      fetchResult();
    }
  }

  function values() {
    return {
      example: el("format-example").value,
      name: el("format-name").value,
      display: el("format-display").value || null,
    };
  }

  async function onAnalyze() {
    if (!hasApi()) return;
    Shell.hideBanner();
    el("format-result").hidden = true;
    const v = values();
    if (
      !Shell.requireFields([
        [v.example, "the example export to learn from", "format-example"],
        [v.name, "a short name for this format", "format-name"],
      ])
    ) {
      return;
    }
    setStep("Step 1 of 2 — looking at the example…");
    setAnalyzing(true);
    try {
      const started = await window.pywebview.api.source_init_async(v.example, v.name, v.display, false);
      if (started && started.ok === false) {
        Shell.showBanner(Shell.refusalText(started.error, "The example could not be read"));
        setStep("Step 1 of 2 — look at the example.");
        setAnalyzing(false);
      }
    } catch (err) {
      Shell.showBanner(String(err));
    }
  }

  async function onSave() {
    if (!hasApi() || !el("format-confirm").checked) return;
    Shell.hideBanner();
    const v = values();
    setStep("Step 2 of 2 — checking that no column would be lost…");
    try {
      const started = await window.pywebview.api.source_init_async(v.example, v.name, v.display, true);
      if (started && started.ok === false) {
        Shell.showBanner(Shell.refusalText(started.error, "The format could not be saved"));
        setStep("Step 2 of 2 — review and confirm.");
      }
    } catch (err) {
      Shell.showBanner(String(err));
    }
  }

  function init() {
    el("format-analyze").addEventListener("click", onAnalyze);
    el("format-save").addEventListener("click", onSave);
    el("format-confirm").addEventListener("change", () => {
      el("format-save").disabled = !el("format-confirm").checked;
    });
    Shell.onReady((live) => {
      el("format-analyze").disabled = !live;
    });
  }

  Shell.registerFlow("source_init", onEvent);
  init();
})();
