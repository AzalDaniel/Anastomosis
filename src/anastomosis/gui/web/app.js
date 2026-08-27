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
  function renderRail() {
    const rail = el("charts-rail");
    if (!rail) return;
    rail.innerHTML = "";
    for (const stage of RAIL) {
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
      rail.appendChild(card);
    }
  }

  function markStage(stage, state) {
    const card = el(`charts-stage-${stage}`);
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

  function resetRun() {
    setCurrent("Rebuilding…");
    renderRail();
    const fill = el("charts-fill");
    if (fill) fill.style.width = "0%";
  }

  function finishRun() {
    setBusy(false);
    const fill = el("charts-fill");
    if (fill) fill.style.width = "100%";
  }

  // --- the event handler for the "pipeline" flow ---------------------------
  function onEvent(event) {
    switch (event.type) {
      case "stage":
        markStage(event.stage, event.state);
        if (event.state === "start") setCurrent(`${Shell.stageLabel(event.stage)}…`);
        break;
      case "progress": {
        const card = el(`charts-stage-${event.stage}`);
        const counts = card && card.querySelector(".stage-counts");
        if (counts) counts.textContent = countsText(event);
        break;
      }
      case "done":
        setCurrent("Finished.");
        finishRun();
        Shell.loadPatients(el("charts-patients"), el("charts-patients-body"), event.summary_id);
        break;
      case "error":
        markStage(event.stage, "error");
        setCurrent("Stopped.");
        Shell.showBanner(`The rebuild stopped during ${Shell.stageLabel(event.stage)}: ${event.error}`);
        finishRun();
        break;
      default:
        break;
    }
  }

  // --- running --------------------------------------------------------------
  async function onRun() {
    if (!hasApi() || !FORM) return;
    Shell.hideBanner();
    Shell.clearPatients(el("charts-patients"), el("charts-patients-body"));
    resetRun();
    setBusy(true);
    const v = FORM.values();
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
        Shell.showBanner(started.error);
        setBusy(false);
        setCurrent("Ready.");
      }
    } catch (err) {
      Shell.showBanner(String(err));
      setBusy(false);
      setCurrent("Ready.");
    }
  }

  // --- populating from info() ----------------------------------------------
  function renderSections(packName) {
    if (!FORM) return;
    FORM.setSections(
      SECTIONS_BY_PACK[packName] || {},
      "This layout has no sections to choose from."
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
      entries.push({ value: layout.name, label: layout.name });
    }
    Shell.fillSelect(pack, entries);
    renderSections(pack ? pack.value : "");
    Shell.fillSelect(FORM.el("source"), [
      { value: "", label: "Detect" },
      ...(info.sources || []).map((src) => ({ value: src.name, label: src.name })),
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
      el("freshness-body").textContent =
        `The filing assistant for ${names} was last checked more than ` +
        `${res.stale_after_days} days ago and may no longer match the destination. ` +
        "Set it up again before filing charts there.";
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
