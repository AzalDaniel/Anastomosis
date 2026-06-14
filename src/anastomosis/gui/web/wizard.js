/*
 * Anastomosis Migration Wizard — vanilla JS, no frameworks, no build step.
 *
 * Talks to the headless controller over pywebview's bridge:
 *   - info()                 → version + sources + packs (the from/render pickers)
 *   - detect(dir)            → step 1 auto-detect
 *   - routes()               → the destination list for the picker
 *   - destination_status(n)  → the transit map, pack readiness, route choice
 *   - run_migration_async(…) → run the migration (charts + structured C-CDA)
 *   - last_run_summary()     → the per-patient roll-up, fetched after `done`
 *
 * The transit map is the centerpiece: three route cards (vendor API / C-CDA
 * import / browser) drawn from destination_status(); the chosen route is
 * highlighted with the coral halo accent. Step 3's guidance is route-specific.
 * The run flow (step 3b) issues run_migration_async and streams progress back
 * via window.anastEvent, then fetches last_run_summary on `done`. The visual
 * layer (glass cards, route cards, command palette) is carried from the
 * predecessor via window.AnastShell. The controller seam is untouched.
 *
 * PHI discipline: events carry counts/stage names only; the per-patient detail
 * (names/DOB) is fetched via last_run_summary and rendered with textContent —
 * shown locally, never logged, never put on an event (the controller's rule).
 * Guarded so opening in a plain browser shows the "launch via anast gui" notice.
 */
"use strict";

const Shell = window.AnastShell;

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

function setStep(n) {
  for (let i = 1; i <= 3; i++) {
    const dot = el("step-" + i);
    if (dot) {
      dot.setAttribute("data-active", i <= n ? "true" : "false");
    }
  }
}

// --- step 1: source auto-detect -------------------------------------------
async function onDetect() {
  if (!hasApi()) {
    return;
  }
  const dir = el("export-dir").value;
  const result = el("detect-result");
  result.hidden = false;
  try {
    const res = await window.pywebview.api.detect(dir);
    if (res && res.ok && res.source) {
      result.textContent = "Detected source format: " + res.source;
      // Pre-select the detected source in the migrate FROM picker (the operator
      // can still override it — a migration's source is always explicit).
      const sel = el("source");
      if (sel) {
        sel.value = res.source;
      }
      setStep(2);
    } else if (res && res.ok) {
      result.textContent = "No known source format detected in that directory.";
    } else {
      result.textContent = "Detection failed: " + (res ? res.error : "no response");
    }
  } catch (err) {
    showBanner(err);
  }
}

// --- step 2: destination + transit map ------------------------------------
const ROUTE_LABEL = {
  vendor_api: "Vendor API",
  ccda_import: "C-CDA import",
  browser: "Browser automation",
};

function renderTransitMap(transit) {
  const map = el("transit-map");
  map.innerHTML = "";
  for (const opt of transit.options) {
    const card = document.createElement("div");
    card.className = "route-card";
    card.setAttribute("data-viable", String(opt.viable));
    card.setAttribute("data-chosen", String(opt.kind === transit.chosen));

    const kind = document.createElement("div");
    kind.className = "kind";
    kind.textContent = ROUTE_LABEL[opt.kind] || opt.kind;
    card.appendChild(kind);

    const why = document.createElement("div");
    why.className = "why";
    why.textContent = opt.why;
    card.appendChild(why);

    const mark = document.createElement("div");
    mark.className = "mark";
    mark.textContent = opt.viable ? "viable" : "not viable";
    card.appendChild(mark);

    if (opt.requires && opt.requires.length) {
      const ul = document.createElement("ul");
      ul.className = "requires";
      for (const req of opt.requires) {
        const li = document.createElement("li");
        li.textContent = req;
        ul.appendChild(li);
      }
      card.appendChild(ul);
    }
    map.appendChild(card);
  }
}

function renderPackChip(pack) {
  const chip = el("pack-chip");
  if (!pack) {
    chip.hidden = true;
    return;
  }
  chip.hidden = false;
  chip.setAttribute("data-ready", String(!!pack.ready));
  chip.textContent = pack.ready
    ? "Browser pack " + pack.name + " ready (selectors discovered)"
    : "Browser pack " + pack.name + " needs discovery";
}

function renderAction(transit, pack) {
  const guidance = el("action-guidance");
  guidance.innerHTML = "";
  const chosen = transit.chosen;
  const lines = [];
  if (chosen === "ccda_import") {
    lines.push(
      "Recommended route: C-CDA import. Run the pipeline with the C-CDA " +
        "deliverer to generate the document the destination ingests:"
    );
    lines.push("    anast pipeline run <export> -o out --ccda");
    lines.push(
      "Then follow the destination's in-product import instructions " +
        "(see the route card evidence above)."
    );
  } else if (chosen === "vendor_api") {
    lines.push(
      "Recommended route: vendor API (the chosen card names which API). " +
        "API push wiring lives in deliver/fhir_api — credentials required."
    );
    lines.push(
      "The full API-run UI is part of a later milestone; for now this is text " +
        "guidance only — no live push from this screen."
    );
  } else if (chosen === "browser") {
    lines.push(
      "Recommended route: browser automation. Open the Upload console to " +
        "stage and review the run."
    );
    if (pack && !pack.ready) {
      lines.push(
        "This destination's browser pack still needs discovery — run " +
          "anast destination init before driving uploads."
      );
    }
  } else {
    lines.push(
      "No viable route: every option is unverified or unsupported. " +
        "Contribute evidence or re-run the registry re-verification ritual."
    );
  }
  for (const line of lines) {
    const p = document.createElement("div");
    p.textContent = line;
    guidance.appendChild(p);
  }
  if (chosen === "vendor_api") {
    const tag = document.createElement("span");
    tag.className = "deferred";
    tag.textContent = "live API push: later milestone";
    guidance.appendChild(tag);
  }
  if (chosen === "browser") {
    const link = document.createElement("a");
    link.className = "nav-link";
    link.href = "console.html";
    link.textContent = "Open Upload console";
    guidance.appendChild(link);
  }
}

async function onDestinationChange() {
  if (!hasApi()) {
    return;
  }
  const name = el("destination").value;
  if (!name) {
    return;
  }
  try {
    const res = await window.pywebview.api.destination_status(name);
    if (!res || !res.ok) {
      showBanner(res ? res.error : "no response");
      return;
    }
    renderTransitMap(res.transit);
    renderPackChip(res.pack);
    renderAction(res.transit, res.pack);
    setStep(3);
    setStatus("route: " + (res.transit.chosen || "none"));
  } catch (err) {
    showBanner(err);
  }
}

// --- step 3b: run the migration -------------------------------------------
// The event dispatcher the shell (Python side) calls during an async run.
window.anastEvent = function anastEvent(e) {
  if (!e || typeof e !== "object") {
    return;
  }
  switch (e.type) {
    case "stage":
      setStatus("stage " + e.stage + ": " + e.state);
      break;
    case "progress":
      setStatus("progress " + e.stage);
      break;
    case "done":
      setRunBusy(false);
      showMigrationResult("migration complete — charts + C-CDA payload written.");
      loadPatients();
      break;
    case "error":
      setRunBusy(false);
      showBanner("Run failed: " + e.error);
      break;
    default:
      break;
  }
};

function setRunBusy(busy) {
  const btn = el("run-migration-btn");
  if (btn) {
    btn.disabled = busy;
    btn.textContent = busy ? "running…" : "run migration";
  }
  setStatus(busy ? "running" : "ready");
}

function showMigrationResult(text) {
  const box = el("migration-result");
  if (box) {
    box.hidden = false;
    box.textContent = text;
  }
}

async function onRunMigration() {
  if (!hasApi()) {
    return;
  }
  const source = el("source") ? el("source").value : "";
  const destination = el("destination") ? el("destination").value : "";
  const render = el("render") ? el("render").value : "neutral";
  const exportDir = el("export-dir") ? el("export-dir").value : "";
  const outDir = el("out-dir") ? el("out-dir").value : "";
  if (!source) {
    showBanner("Pick a source EHR (migrate FROM) first.");
    return;
  }
  if (!destination) {
    showBanner("Pick a destination EHR (migrate TO) first.");
    return;
  }
  clearPatients();
  setRunBusy(true);
  showMigrationResult("running…");
  try {
    const started = await window.pywebview.api.run_migration_async(
      exportDir,
      outDir,
      source,
      destination,
      render
    );
    if (started && started.ok === false) {
      showBanner(started.error);
      setRunBusy(false);
    }
  } catch (err) {
    showBanner(err);
    setRunBusy(false);
  }
}

// --- per-patient detail (local display; never on an event) ----------------
async function loadPatients() {
  if (!hasApi()) {
    return;
  }
  try {
    const res = await window.pywebview.api.last_run_summary();
    if (res && res.ok) {
      renderPatients(res.patients || []);
    }
  } catch (err) {
    // The summary is advisory; never block the run roll-up on it.
  }
}

function clearPatients() {
  const body = el("patients-body");
  if (body) {
    body.innerHTML = "";
  }
  const panel = el("patients-panel");
  if (panel) {
    panel.hidden = true;
  }
}

function renderPatients(patients) {
  const panel = el("patients-panel");
  const body = el("patients-body");
  if (!panel || !body) {
    return;
  }
  body.innerHTML = "";
  if (!patients.length) {
    panel.hidden = true;
    return;
  }
  const table = document.createElement("table");
  table.className = "patients-table";
  const head = document.createElement("tr");
  for (const heading of ["patient", "dob", "encounters", "notes"]) {
    const th = document.createElement("th");
    th.textContent = heading;
    head.appendChild(th);
  }
  table.appendChild(head);
  for (const p of patients) {
    const tr = document.createElement("tr");
    const cells = [
      p.display_name || "—",
      p.birth_date || "—",
      String(p.encounters),
      String(p.documents),
    ];
    for (const value of cells) {
      const td = document.createElement("td");
      td.textContent = value; // textContent: PHI rendered as text, never HTML
      tr.appendChild(td);
    }
    table.appendChild(tr);
  }
  body.appendChild(table);
  panel.hidden = false;
}

// --- bootstrap ------------------------------------------------------------
function populateSources(sources) {
  const select = el("source");
  if (!select) {
    return;
  }
  select.innerHTML = "";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = "Select a source…";
  select.appendChild(blank);
  for (const src of sources) {
    const opt = document.createElement("option");
    opt.value = src.name;
    opt.textContent = src.name;
    select.appendChild(opt);
  }
}

// The render picker: the two named modes plus every available pack.
function populateRender(packs) {
  const select = el("render");
  if (!select) {
    return;
  }
  select.innerHTML = "";
  const named = [
    ["neutral", "neutral (generic SOAP)"],
    ["ccda-standard", "ccda-standard (HL7 view)"],
  ];
  for (const [value, label] of named) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    select.appendChild(opt);
  }
  for (const pack of packs || []) {
    if (!pack.available) {
      continue;
    }
    const opt = document.createElement("option");
    opt.value = pack.name;
    opt.textContent = "pack: " + pack.name;
    select.appendChild(opt);
  }
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
      populateSources(info.sources || []);
      populateRender(info.packs || []);
    }
    const routes = await window.pywebview.api.routes();
    if (routes && routes.ok) {
      const select = el("destination");
      select.innerHTML = "";
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "Select a destination…";
      select.appendChild(blank);
      for (const r of routes.routes) {
        const opt = document.createElement("option");
        opt.value = r.destination;
        opt.textContent = r.destination;
        select.appendChild(opt);
      }
    }
  } catch (err) {
    showBanner(err);
  }
}

function init() {
  const detect = el("detect-btn");
  if (detect) {
    detect.addEventListener("click", onDetect);
  }
  const dest = el("destination");
  if (dest) {
    dest.addEventListener("change", onDestinationChange);
  }
  const runBtn = el("run-migration-btn");
  if (runBtn) {
    runBtn.addEventListener("click", onRunMigration);
  }

  const palette = Shell.initCommandPalette([
    { id: "detect", label: "Auto-detect source", hint: "step 1", action: () => onDetect() },
    { id: "run-migration", label: "Run migration", hint: "run", action: () => onRunMigration() },
    { id: "dashboard", label: "Open dashboard", hint: "go", action: () => { window.location.href = "index.html"; } },
    { id: "console", label: "Open upload console", hint: "go", action: () => { window.location.href = "console.html"; } },
    { id: "packgen", label: "Open pack from samples", hint: "go", action: () => { window.location.href = "packgen.html"; } },
  ]);
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      palette.toggle();
    }
  });

  populate();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
