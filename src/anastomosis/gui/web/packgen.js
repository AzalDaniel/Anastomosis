/*
 * Anastomosis GUI — Teach, mode 1: teach a document layout from samples.
 *
 * Owns the "pack_init" flow and registers the Teach view itself (mode 2,
 * export formats, lives in source.js and registers only its own flow).
 *
 * The two-step gate is learn.js's; this file is the descriptor for it plus
 * what only this mode paints.
 *
 * PHI discipline: the summary carries static template text (recurring across
 * distinct samples) and counts only; sample paths are never echoed.
 */
"use strict";

(function () {
  const Shell = window.AnastShell;
  const el = (id) => document.getElementById(id);

  const FAILED = "The samples could not be turned into a layout";

  function renderProposal(res) {
    el("layout-summary").textContent = (res.summary || []).join("\n");
    el("layout-caveat").textContent = res.caveat
      ? `Before you confirm: ${res.caveat}`
      : "";
  }

  function renderWritten(res, ui) {
    ui.showResult(`The draft layout was written to ${res.pack_dir}`, res.draft_md);
    ui.setStep(
      res.pack
        ? `Done. "${res.pack}" is now offered on Charts and Migrate — review the draft against an original sample before using it.`
        : "Done. Review the draft against an original sample before using it."
    );
    // The layout exists NOW, so the lists that offer layouts are asked again
    // NOW. Without this the choosers keep the list they were populated with at
    // boot, and the layout this view just wrote is unselectable until restart.
    Shell.reloadInfo();
  }

  const wizard = window.AnastLearn.wizard({
    mode: "layout",
    stage: "packgen",
    input: "layout-samples",
    write: "layout-write",
    needs: {
      input: "the folder your sample charts are in",
      name: "a short name for this layout",
    },
    say: {
      looking: "Step 1 of 2 — looking at the samples…",
      look: "Step 1 of 2 — look at the samples.",
      review: "Step 2 of 2 — review and confirm.",
      writing: "Step 2 of 2 — writing the draft…",
      unreadable: "The samples could not be read",
      unwritable: "The draft layout could not be written",
      failed: FAILED,
    },
    start: (v, confirmed) =>
      window.pywebview.api.pack_init_async(v.input, v.name, v.display, confirmed),
    last: () => window.pywebview.api.last_pack_result(),
    renderProposal,
    renderWritten,
    // This mode has no pointed refusals to anchor, so an error event says what
    // it carries rather than costing a fetch for the same sentence.
    onErrorEvent: (event) => Shell.showBanner(`${FAILED}: ${event.error}`),
  });

  // "Teach it another" needs an "another than WHAT". This is that: the formats
  // and layouts already installed, as static rows — no tint, because every row
  // here has the same status and a tint that never varies carries nothing.
  function renderKnown(info) {
    const summary = el("teach-known-summary");
    const list = el("teach-known");
    if (!summary || !list) return;
    const sources = info.sources || [];
    const layouts = (info.packs || []).filter((pack) => pack.available);
    const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;
    summary.textContent =
      `Anastomosis reads ${count(sources.length, "export format", "export formats")} ` +
      `and lays charts out ${count(layouts.length, "way", "ways")}. ` +
      "Teach it another when it meets one it does not know.";

    list.innerHTML = "";
    for (const source of sources) {
      const row = Shell.resultRow(null, [
        { text: Shell.nameOf(source) },
        { text: source.description || "", className: "result-note" },
      ]);
      row.title = source.name;
      list.appendChild(row);
    }
    for (const layout of layouts) {
      const row = Shell.resultRow(null, [
        { text: Shell.nameOf(layout) },
        { text: "Chart layout", className: "result-note" },
      ]);
      row.title = layout.name;
      list.appendChild(row);
    }
  }

  Shell.onInfo(renderKnown);

  // The Teach VIEW is registered here (one workspace, two modes); source.js
  // registers only the second mode's flow.
  Shell.registerView({
    name: "teach",
    title: "Teach",
    flow: "pack_init",
    onEvent: wizard.onEvent,
  });
})();
