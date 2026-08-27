/*
 * Anastomosis GUI — the Migrate view: move charts from one system into another.
 *
 * Owns the "migration" flow. It composes the SAME run form Charts uses
 * (shell.js buildRunForm) beside a destination picker — there is no second
 * implementation of the run fields — and adds what only a migration needs: the
 * routes into the destination, in plain language, and the handoff that carries
 * the chosen route's context to Uploads instead of asking for it twice.
 *
 * Controller seam: detect / routes / destination_status / run_migration_async /
 * last_run_summary. PHI discipline: events carry counts and stage names; the
 * per-patient roll-up is fetched separately and painted with textContent.
 */
"use strict";

(function () {
  const Shell = window.AnastShell;
  const el = (id) => document.getElementById(id);

  let SECTIONS_BY_PACK = {};
  let FORM = null;
  //: The destination's filing assistant, from destination_status() — the thing
  //: the Uploads handoff pre-fills.
  let ASSISTANT = null;

  // Route kinds in the operator's language. The registry's own `why` evidence
  // is kept verbatim, but under a "Technical detail" disclosure — nothing is
  // dropped, and nothing engineering-shaped leads.
  const ROUTE_NAME = {
    vendor_api: "Direct connection",
    ccda_import: "Transfer document (C-CDA)",
    browser: "Filing assistant",
  };
  const ROUTE_WHAT = {
    vendor_api: "Sends charts straight into the destination's own interface.",
    ccda_import: "Creates one transfer document per patient that the destination can import.",
    browser: "Anastomosis files each chart into the destination itself, through a browser window it controls.",
  };

  function hasApi() {
    return Shell.hasApi();
  }

  // --- the routes into a destination ---------------------------------------
  function renderRoutes(transit) {
    const list = el("migrate-routes");
    list.innerHTML = "";
    for (const opt of transit.options) {
      const card = document.createElement("div");
      card.className = "route-card";
      card.dataset.available = String(opt.viable);
      card.dataset.chosen = String(opt.kind === transit.chosen);
      card.dataset.route = opt.kind;

      const name = document.createElement("div");
      name.className = "route-name";
      name.textContent = ROUTE_NAME[opt.kind] || opt.kind;
      card.appendChild(name);

      const what = document.createElement("div");
      what.className = "route-why";
      what.textContent = ROUTE_WHAT[opt.kind] || "";
      card.appendChild(what);

      const mark = document.createElement("div");
      mark.className = "route-mark";
      mark.textContent = opt.viable
        ? opt.kind === transit.chosen
          ? "Available — recommended"
          : "Available"
        : "Not available";
      card.appendChild(mark);

      // The registry's evidence, verbatim and reachable, but never the headline.
      const detail = document.createElement("details");
      detail.className = "route-detail";
      const summary = document.createElement("summary");
      summary.textContent = "Technical detail";
      detail.appendChild(summary);
      const why = document.createElement("div");
      why.textContent = opt.why;
      detail.appendChild(why);
      for (const req of opt.requires || []) {
        const line = document.createElement("div");
        line.textContent = req;
        detail.appendChild(line);
      }
      card.appendChild(detail);
      list.appendChild(card);
    }
  }

  function paragraphs(host, lines) {
    host.innerHTML = "";
    for (const line of lines) {
      const p = document.createElement("p");
      p.textContent = line;
      host.appendChild(p);
    }
  }

  function renderGuidance(transit, pack) {
    const guidance = el("migrate-guidance");
    const chosen = transit.chosen;
    const lines = [];
    if (chosen === "ccda_import") {
      lines.push(
        "This route creates a C-CDA transfer document the destination can import. " +
          "“Rebuild charts” below produces it."
      );
      lines.push("Then follow the destination's own import instructions.");
    } else if (chosen === "vendor_api") {
      lines.push(
        "This route sends charts directly to the destination's FHIR interface. It " +
          "needs sign-in credentials from your destination system, and runs from " +
          "the Uploads screen."
      );
      lines.push("Direct sending is not available from this screen yet.");
    } else if (chosen === "browser") {
      lines.push(
        "Anastomosis can file these charts into the destination itself. Rebuild the " +
          "charts below, then continue on the Uploads screen."
      );
    } else {
      lines.push(
        "No route to this destination is available yet. Routes appear here once " +
          "they have been verified to work."
      );
    }
    if (pack) {
      lines.push(
        pack.ready
          ? `Filing assistant for ${pack.name} is ready.`
          : "The filing assistant for this system has not been set up on this " +
            "computer yet. Set it up from the Teach screen."
      );
    }
    paragraphs(guidance, lines);
    // The handoff is offered whenever a filing assistant exists for this
    // destination — that is the context Uploads would otherwise ask for again.
    el("migrate-handoff-actions").hidden = !pack;
  }

  async function onDestinationChange() {
    if (!hasApi()) return;
    const name = el("migrate-destination").value;
    if (!name) return;
    try {
      const res = await window.pywebview.api.destination_status(name);
      if (!res || !res.ok) {
        Shell.showBanner(res ? res.error : "The destination could not be read.");
        return;
      }
      ASSISTANT = res.pack || null;
      renderRoutes(res.transit);
      renderGuidance(res.transit, res.pack);
    } catch (err) {
      Shell.showBanner(String(err));
    }
  }

  function onContinueOnUploads() {
    const context = {
      destination: el("migrate-destination").value,
      assistant: ASSISTANT ? ASSISTANT.name : "",
      outDir: FORM ? FORM.values().outDir : "",
    };
    // The context IS the handoff. It used to be written to a shell-global as
    // well, and Uploads read that global on every arrival — so the offer never
    // expired and kept overwriting fields the operator had since retyped.
    Shell.showView("uploads", context);
  }

  // --- detect the export format --------------------------------------------
  async function onDetect() {
    if (!hasApi() || !FORM) return;
    const dir = FORM.values().exportDir;
    try {
      const res = await window.pywebview.api.detect(dir);
      if (res && res.ok && res.source) {
        FORM.el("source").value = res.source;
        Shell.logEvent({ kind: "ok", msg: `Migrate: this export looks like ${res.source}.` });
      } else if (res && res.ok) {
        Shell.logEvent({ kind: "warn", msg: "Migrate: this folder is not a format Anastomosis knows." });
      } else {
        Shell.showBanner(
          `The export folder could not be read: ${res ? res.error : "no answer from the app"}`
        );
      }
    } catch (err) {
      Shell.showBanner(String(err));
    }
  }

  // --- running --------------------------------------------------------------
  function showResult(text) {
    const box = el("migrate-result");
    box.hidden = false;
    box.innerHTML = "";
    const p = document.createElement("p");
    p.textContent = text;
    box.appendChild(p);
  }

  // A run asked for but not yet begun. The click answers straight away, but the
  // last run's patient table is a RESULT and stays until this run has its own —
  // so a submit the controller refuses puts the result box back and leaves the
  // finished run alone. See onRun.
  let pendingRun = null;

  function askForRun() {
    const box = el("migrate-result");
    pendingRun = { hidden: box.hidden, text: box.textContent };
    showResult("Rebuilding…");
  }

  function abandonRun() {
    if (!pendingRun) return;
    const box = el("migrate-result");
    if (pendingRun.hidden) box.hidden = true;
    else showResult(pendingRun.text);
    pendingRun = null;
  }

  function beginRun() {
    if (!pendingRun) return;
    pendingRun = null;
    Shell.clearPatients(el("migrate-patients"), el("migrate-patients-body"));
  }

  async function onRun() {
    if (!hasApi() || !FORM) return;
    const v = FORM.values();
    const destination = el("migrate-destination").value;
    if (!v.source) {
      Shell.showBanner("Choose the export format these charts are coming from.");
      return;
    }
    if (!destination) {
      Shell.showBanner("Choose the system these charts are going to.");
      return;
    }
    Shell.hideBanner();
    askForRun();
    FORM.setBusy(true);
    try {
      const started = await window.pywebview.api.run_migration_async(
        v.exportDir,
        v.outDir,
        v.source,
        destination,
        v.render,
        v.sections,
        v.qa,
        v.force,
        v.packDirs,
        v.trustNew
      );
      if (started && started.ok === false) {
        abandonRun();
        FORM.setBusy(false);
        Shell.showBanner(Shell.refusalText(started.error));
      }
    } catch (err) {
      abandonRun();
      FORM.setBusy(false);
      Shell.showBanner(String(err));
    }
  }


  function onEvent(event) {
    beginRun();
    switch (event.type) {
      case "done":
        FORM.setBusy(false);
        // The controller's own `notice` is written for the terminal; the honest
        // verdict in this register is fixed, so it is said here instead.
        showResult(
          "Charts and the transfer document are written. Nothing has been sent yet " +
            "— review the results, then continue on the Uploads screen."
        );
        Shell.loadPatients(el("migrate-patients"), el("migrate-patients-body"), event.summary_id);
        break;
      case "error":
        FORM.setBusy(false);
        // A deliver-stage error in this flow is the no-automatic-route verdict:
        // the charts and the transfer document WERE written (the console emits
        // it in place of `done`), so it is reported as an outcome, not a crash.
        if (event.stage === "deliver") {
          showResult(
            "No automatic route to this destination is available. The charts and the " +
              "transfer document are written — import the transfer document into the " +
              "destination yourself, or set up a filing assistant from the Teach screen."
          );
          Shell.showBanner("The migration finished without sending anything. See the note below.");
        } else {
          Shell.showBanner(
            `The migration stopped during ${Shell.stageLabel(event.stage)}: ${event.error}`
          );
        }
        break;
      default:
        break;
    }
  }

  // --- populating -----------------------------------------------------------
  function renderSections(renderValue) {
    // "ccda-standard" is the HL7 data view (no page layout, so no sections);
    // "neutral" renders through the generic layout; anything else IS a layout.
    const layout =
      renderValue === "ccda-standard" ? null : renderValue === "neutral" ? "generic_soap" : renderValue;
    // The third argument is what makes a choice stick. Without it this view
    // remembered nothing, so every change of the "Chart pages" picker put the
    // layout's defaults back — and the run was submitted from the reinstated
    // values, not the physician's. Charts got this in #129; Migrate calls the
    // same form and needs the same key.
    FORM.setSections(
      (layout && SECTIONS_BY_PACK[layout]) || {},
      layout === null
        ? "Data-only transfer documents have no sections to choose from."
        : "This layout has no sections to choose from.",
      layout
    );
  }

  function populate(info) {
    if (!FORM) return;
    Shell.fillChooser(FORM.el("source"), [
      { value: "", label: "Choose the export format…" },
      ...(info.sources || []).map((src) => ({
        value: src.name,
        label: Shell.displayName(src.name),
        note: src.description || src.name,
      })),
    ]);
    SECTIONS_BY_PACK = {};
    const layouts = [];
    for (const layout of info.packs || []) {
      if (!layout.available) continue;
      SECTIONS_BY_PACK[layout.name] = layout.sections || {};
      layouts.push({
        value: layout.name,
        label: `Rendered pages — ${Shell.displayName(layout.name)}`,
        note: layout.name,
      });
    }
    Shell.fillChooser(FORM.el("render"), [
      { value: "neutral", label: "Rendered pages — standard layout", note: "neutral" },
      { value: "ccda-standard", label: "Data only — C-CDA", note: "ccda-standard" },
      ...layouts,
    ]);
    renderSections(FORM.el("render").value);
  }

  async function loadRoutes() {
    try {
      const routes = await window.pywebview.api.routes();
      if (routes && routes.ok) {
        Shell.fillChooser(el("migrate-destination"), [
          { value: "", label: "Choose a destination…" },
          ...routes.routes.map((r) => ({
            value: r.destination,
            label: Shell.displayName(r.destination),
            note: r.destination,
          })),
        ]);
      }
    } catch (err) {
      Shell.showBanner(String(err));
    }
  }

  function init() {
    FORM = Shell.buildRunForm(el("migrate-form"), {
      prefix: "migrate",
      mode: "migrate",
      runLabel: "Rebuild charts",
      onRun,
    });
    const detect = FORM.el("detect");
    if (detect) detect.addEventListener("click", onDetect);
    const render = FORM.el("render");
    if (render) render.addEventListener("change", () => renderSections(render.value));
    el("migrate-destination").addEventListener("change", onDestinationChange);
    el("migrate-continue").addEventListener("click", onContinueOnUploads);

    Shell.onInfo(populate);
    Shell.onReady((live) => {
      if (!live) {
        FORM.setOffline();
        return;
      }
      FORM.setBusy(false);
      loadRoutes();
    });
  }

  Shell.registerView({
    name: "migrate",
    title: "Migrate",
    flow: "migration",
    onEvent,
  });
  init();
})();
