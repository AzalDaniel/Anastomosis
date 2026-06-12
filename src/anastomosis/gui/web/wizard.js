/*
 * Anastomosis Migration Wizard — vanilla JS, no frameworks, no build step.
 *
 * Talks to the headless controller over pywebview's bridge:
 *   - info()                 → version + the destination list (registry names)
 *   - detect(dir)            → step 1 auto-detect
 *   - destination_status(n)  → the transit map, pack readiness, route choice
 *
 * The transit map is the centerpiece: three route cards (vendor API / C-CDA
 * import / browser) drawn from destination_status(); the chosen route is
 * highlighted with the halo accent. Step 3's guidance is route-specific.
 *
 * PHI discipline: every value rendered here is a destination name, a capability
 * kind, an evidence date, a pack name, or a boolean — never patient-derived.
 * Guarded so opening in a plain browser shows the "launch via anast gui" notice.
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
  } catch (err) {
    showBanner(err);
  }
}

// --- bootstrap ------------------------------------------------------------
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
  populate();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
