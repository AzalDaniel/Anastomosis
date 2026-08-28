/*
 * Anastomosis GUI — the Charts view: turn an EHR export into complete,
 * verified charts.
 *
 * Owns the "pipeline" flow. The run form is the SHARED component built by
 * shell.js (Migrate composes the same one); this file owns only what is
 * specific to a plain rebuild: which layout's sections apply, the four stages,
 * and the per-patient roll-up after a run.
 *
 * Talks to the headless controller over pywebview's bridge; progress arrives
 * the other way through the shell's one `window.anastEvent` dispatcher.
 *
 * PHI discipline mirrors the controller: this view renders counts, stage names
 * and exception type names. The per-patient detail is fetched separately and
 * painted with textContent — shown locally, never logged, never on an event.
 */
"use strict";

(function () {
  const Shell = window.AnastShell;
  const el = (id) => document.getElementById(id);

  // The stage rail. This FALLBACK is only for the api-less browser preview; the
  // live list is refreshed from the Python-canonical gui_config() endpoint, and
  // tests/unit/test_frontend_constants.py pins the fallback to the Python list
  // so neither side can drift alone.
  let RAIL = ["ingest", "reconstruct", "qa", "deliver"];

  // Per-layout section maps (name -> {section: {label, default}}), from info().
  let SECTIONS_BY_PACK = {};
  let FORM = null;

  function hasApi() {
    return Shell.hasApi();
  }

  async function loadGuiConfig() {
    if (!hasApi() || typeof window.pywebview.api.gui_config !== "function") return;
    try {
      const cfg = await window.pywebview.api.gui_config();
      if (cfg && cfg.ok && Array.isArray(cfg.stage_rail) && cfg.stage_rail.length) {
        RAIL = cfg.stage_rail.map(String);
        renderRail();
      }
    } catch (_) {
      /* keep the fallback */
    }
  }

  // --- the stage rail -------------------------------------------------------
  // Which stages THIS run will actually perform. The double-check only runs when
  // it is on, and "Saving results" only when an extra artifact was asked for —
  // so advertising all four regardless left one stage (two, with the
  // double-check off) sitting grey forever under a status line reading
  // "Finished.", which reads as "your charts were not saved" when they were.
  // Called with no config before a run, where the full shape is the preview.
  function stagesFor(config) {
    if (!config) return RAIL;
    return RAIL.filter((stage) => {
      if (stage === "qa") return config.qa !== false;
      if (stage === "deliver") return !!(config.archive || config.bundle || config.ccda);
      return true;
    });
  }

  function makeStageCard(stage) {
      const card = document.createElement("div");
      card.className = "stage";
      card.id = `charts-stage-${stage}`;
      const head = document.createElement("div");
      head.className = "stage-name";
      const mark = document.createElement("span");
      mark.className = "icon stage-icon";
      mark.appendChild(Shell.icon("waiting"));
      const name = document.createElement("span");
      // Plain-English stage names; the technical id rides the tooltip.
      name.textContent = Shell.stageLabel(stage);
      card.title = stage;
      head.appendChild(mark);
      head.appendChild(name);
      const counts = document.createElement("div");
      counts.className = "stage-counts";
      card.appendChild(head);
      card.appendChild(counts);
      return card;
  }

  function renderRail(config) {
    const rail = el("charts-rail");
    if (!rail) return;
    rail.innerHTML = "";
    for (const stage of stagesFor(config)) rail.appendChild(makeStageCard(stage));
  }

  // The rail is built from what the run SAID it would do; an event is what it
  // actually did. If they disagree, reality wins — a stage that reports itself
  // gets a card rather than having its counts vanish into a missing element.
  function ensureStageCard(stage) {
    const existing = el(`charts-stage-${stage}`);
    if (existing) return existing;
    const rail = el("charts-rail");
    if (!rail) return null;
    const card = makeStageCard(stage);
    rail.appendChild(card);
    return card;
  }

  function markStage(stage, state) {
    const card = ensureStageCard(stage);
    if (!card) return;
    card.dataset.state = state;
    const mark = card.querySelector(".stage-icon");
    if (mark) {
      mark.textContent = "";
      mark.appendChild(Shell.icon(state === "done" ? "ok" : state === "error" ? "error" : "waiting"));
    }
  }

  const NON_COUNT_KEYS = ["type", "stage", "state", "flow", "summary_id", "notice", "outcome"];
  function countsText(event) {
    return Object.keys(event)
      .filter((k) => !NON_COUNT_KEYS.includes(k))
      .map((k) => `${k.replace(/_/g, " ")} ${event[k]}`)
      .join(" · ");
  }

  function setCurrent(text) {
    const current = el("charts-current");
    if (current) current.textContent = text;
  }

  function setBusy(busy) {
    if (FORM) FORM.setBusy(busy);
    const frame = el("charts-progress");
    if (frame) frame.classList.toggle("is-running", busy);
  }

  // What a stage that could not run tells the person who asked for it.
  // COPY_MAP.md prescribes this sentence for the double-check.
  const SKIPPED_NOTE = {
    qa: "This installation cannot double-check charts. Reinstall the full package to enable it.",
  };
  let skippedStages = [];

  // --- the progress bar -----------------------------------------------------
  // The bar measures stages settled out of stages planned. It used to sit at 0%
  // for the whole run and then jump to full from the failure branch as well as
  // the finish branch, so a run that stopped on stage two showed a complete,
  // brand-coloured bar directly under the word "Stopped."
  let plannedStages = [];
  let settledStages = 0;

  function setProgress(percent) {
    const fill = el("charts-fill");
    if (fill) fill.style.width = `${percent}%`;
    const frame = el("charts-progress");
    const bar = frame && frame.querySelector(".progress-bar");
    if (bar) bar.setAttribute("aria-valuenow", String(Math.round(percent)));
  }

  // Done and skipped both settle a stage: neither is still pending.
  function advanceProgress() {
    if (!plannedStages.length) return;
    settledStages += 1;
    setProgress(Math.min(100, (settledStages / plannedStages.length) * 100));
  }

  function stopProgress() {
    const frame = el("charts-progress");
    if (frame) frame.classList.add("is-stopped");
  }

  // A run that has been asked for but has not begun. The click says
  // "Rebuilding…" and empties the bar straight away, because a button that
  // answers nothing feels broken — but the last run's rail counts and patient
  // table are RESULTS, and they stay until this run actually produces its own.
  // So a submit the controller refuses puts back the two things the click
  // moved, and the finished run below them is never touched.
  let pendingRun = null;

  function askForRun(config) {
    pendingRun = {
      config,
      current: el("charts-current") ? el("charts-current").textContent : "",
      fill: el("charts-fill") ? el("charts-fill").style.width : "0%",
    };
    setCurrent("Rebuilding…");
    setProgress(0);
  }

  function abandonRun() {
    if (!pendingRun) return;
    setCurrent(pendingRun.current);
    const fill = el("charts-fill");
    if (fill) fill.style.width = pendingRun.fill;
    pendingRun = null;
  }

  // The first event of the run is what replaces the last one's results. Doing it
  // here rather than at submit time also settles the order: an event that beat
  // the bridge's answer back can no longer have its stage card wiped by a reset
  // arriving after it.
  function beginRun() {
    if (!pendingRun) return;
    const config = pendingRun.config;
    pendingRun = null;
    skippedStages = [];
    plannedStages = stagesFor(config);
    settledStages = 0;
    Shell.clearPatients(el("charts-patients"), el("charts-patients-body"));
    const frame = el("charts-progress");
    if (frame) frame.classList.remove("is-stopped");
    renderRail(config);
  }

  // --- the event handler for the "pipeline" flow ---------------------------
  function onEvent(event) {
    beginRun();
    switch (event.type) {
      case "stage":
        markStage(event.stage, event.state);
        if (event.state === "done") advanceProgress();
        if (event.state === "start") setCurrent(`${Shell.stageLabel(event.stage)}…`);
        if (event.state === "skipped") {
          // A stage that did not run must say so where the tick would have been,
          // and again in the banner: a physician who asked for the double-check
          // and saw only a quiet rail would believe their charts were checked.
          skippedStages.push(Shell.stageLabel(event.stage));
          const card = ensureStageCard(event.stage);
          const counts = card && card.querySelector(".stage-counts");
          if (counts) counts.textContent = SKIPPED_NOTE[event.stage] || "did not run";
          advanceProgress();
        }
        break;
      case "progress": {
        const card = ensureStageCard(event.stage);
        const counts = card && card.querySelector(".stage-counts");
        if (counts) counts.textContent = countsText(event);
        break;
      }
      case "done":
        setCurrent(
          skippedStages.length
            ? `Finished, but ${skippedStages.join(" and ")} did not run.`
            : "Finished."
        );
        if (skippedStages.length) Shell.showBanner(SKIPPED_NOTE.qa);
        setBusy(false);
        setProgress(100);
        Shell.loadPatients(el("charts-patients"), el("charts-patients-body"), event.summary_id);
        break;
      case "error":
        markStage(event.stage, "error");
        setCurrent("Stopped.");
        Shell.showBanner(`The rebuild stopped during ${Shell.stageLabel(event.stage)}: ${event.error}`);
        setBusy(false);
        // The bar stays where the run got to, in the stopped colour.
        stopProgress();
        break;
      default:
        break;
    }
  }

  // --- running --------------------------------------------------------------
  async function onRun() {
    if (!hasApi() || !FORM) return;
    Shell.hideBanner();
    // Read the form now, paint from it later: the rail is built from what this
    // run will do, not from what a run could do — but nothing on screen changes
    // until the controller confirms the run is actually under way.
    const v = FORM.values();
    if (
      !Shell.requireFields([
        [v.exportDir, "the folder your export is in", FORM.idFor("export-dir")],
        [v.outDir, "the folder to put the charts in", FORM.idFor("out-dir")],
      ])
    ) {
      return;
    }
    askForRun(v);
    setBusy(true);
    try {
      // Fire-and-forget on a worker thread; results stream back as events.
      const started = await window.pywebview.api.run_pipeline_async(
        v.exportDir,
        v.outDir,
        v.pack,
        v.source,
        v.sections,
        v.qa,
        v.archive,
        v.bundle,
        v.ccda,
        v.force,
        v.packDirs,
        v.trustNew,
        v.writeManifest
      );
      if (started && started.ok === false) {
        // Nothing started, so the screen goes back to describing the last run
        // that did: no rail reset, no wiped patient table, no "Ready." over
        // work that is still in flight somewhere else.
        abandonRun();
        setBusy(false);
        Shell.showBanner(Shell.refusalText(started.error));
      }
    } catch (err) {
      abandonRun();
      setBusy(false);
      Shell.showBanner(String(err));
    }
  }

  // --- populating from info() ----------------------------------------------
  function renderSections(packName) {
    if (!FORM) return;
    FORM.setSections(
      SECTIONS_BY_PACK[packName] || {},
      "This layout has no sections to choose from.",
      packName
    );
  }

  function populate(info) {
    if (!FORM) return;
    const pack = FORM.el("pack");
    SECTIONS_BY_PACK = {};
    const entries = [];
    for (const layout of info.packs || []) {
      if (!layout.available) continue;
      SECTIONS_BY_PACK[layout.name] = layout.sections || {};
      // The name a person reads, with the id they would quote to support
      // underneath it — the two used to compete for one slot, and the id won.
      entries.push({
        value: layout.name,
        label: Shell.displayName(layout.name),
        note: layout.name,
      });
    }
    Shell.fillChooser(pack, entries);
    renderSections(pack ? pack.value : "");
    Shell.fillChooser(FORM.el("source"), [
      { value: "", label: "Detect", note: "works for every built-in format" },
      ...(info.sources || []).map((src) => ({
        value: src.name,
        label: Shell.displayName(src.name),
        note: src.description || src.name,
      })),
    ]);
  }

  // --- the out-of-date filing-assistant notice ------------------------------
  async function checkFreshness() {
    if (!hasApi()) return;
    try {
      const res = await window.pywebview.api.pack_freshness();
      if (!res || !res.ok || !Array.isArray(res.stale) || res.stale.length === 0) return;
      const names = res.stale.map((s) => s.destination).join(", ");
      // The controller's `advice` is a terminal command — never shown here.
      // `pack_freshness` returns the exact command per destination in `advice`;
      // it was being discarded in favour of "set it up again", which named no
      // way to do it. One stale destination gets its command; several get the
      // shape, since the toast is one line.
      const advice =
        res.stale.length === 1 && res.stale[0].advice
          ? `Run: ${res.stale[0].advice}`
          : "Re-check them with: anast destination init <name> --validate";
      el("freshness-body").textContent =
        `The filing assistant for ${names} was last checked more than ` +
        `${res.stale_after_days} days ago and may no longer match the destination. ` +
        `${advice}`;
      el("freshness-toast").classList.add("show");
    } catch (_) {
      /* advisory only; never block the view on it */
    }
  }

  function init() {
    FORM = Shell.buildRunForm(el("charts-form"), {
      prefix: "charts",
      mode: "charts",
      runLabel: "Rebuild charts",
      onRun,
    });
    renderRail();
    const pack = FORM.el("pack");
    if (pack) pack.addEventListener("change", () => renderSections(pack.value));
    const dismiss = el("freshness-dismiss");
    if (dismiss) {
      dismiss.addEventListener("click", () => el("freshness-toast").classList.remove("show"));
    }

    Shell.onInfo(populate);
    Shell.onReady((live) => {
      if (!live) {
        // No bridge: the run button is inert because there is no controller to
        // run against — but it must never claim a run that does not exist.
        FORM.setOffline();
        return;
      }
      FORM.setBusy(false);
      setCurrent("Ready.");
      loadGuiConfig();
      checkFreshness();
    });
  }

  Shell.registerView({
    name: "charts",
    title: "Charts",
    flow: "pipeline",
    onEvent,
  });
  init();
})();
