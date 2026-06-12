/*
 * Anastomosis GUI dashboard — vanilla JS, no frameworks, no build step.
 *
 * Talks to the headless controller (anastomosis.gui.controller.GuiController)
 * over pywebview's bridge: every call is `window.pywebview.api.<method>(...)`,
 * which returns a Promise of a JSON-safe dict. Progress arrives the other way,
 * pushed by the shell as `window.anastEvent(<event>)`.
 *
 * PHI discipline mirrors the controller: this UI renders counts, stage names,
 * ids, and exception type names. It never receives — and so cannot show —
 * patient field values or rendered filenames.
 *
 * Guarded so opening index.html in a PLAIN browser (no pywebview) shows the
 * "launch via anast gui" notice instead of throwing.
 */
"use strict";

// The dashboard's four stage rails, in pipeline order.
const RAIL = ["ingest", "reconstruct", "qa", "deliver"];

function hasApi() {
  return typeof window.pywebview !== "undefined" && !!window.pywebview.api;
}

function el(id) {
  return document.getElementById(id);
}

// --- the event dispatcher the shell calls ---------------------------------
// Defined on window so the shell's evaluate_js("window.anastEvent(...)") finds
// it regardless of module scope.
window.anastEvent = function anastEvent(e) {
  if (!e || typeof e !== "object") {
    return;
  }
  switch (e.type) {
    case "stage":
      markStage(e.stage, e.state);
      logLine(`stage ${e.stage}: ${e.state}`);
      break;
    case "progress":
      renderCounters(e);
      logLine(`progress ${e.stage}: ${counterText(e)}`);
      break;
    case "done":
      logLine(`done: ${counterText(e)}`, "done");
      finishRun();
      break;
    case "error":
      markStage(e.stage, "error");
      showBanner(e.error);
      logLine(`error ${e.stage}: ${e.error}`, "error");
      finishRun();
      break;
    default:
      break;
  }
};

function markStage(stage, state) {
  const card = el(`stage-${stage}`);
  if (card) {
    card.setAttribute("data-state", state);
  }
}

function counterText(e) {
  // Render every integer-valued field except the discriminators as k=v.
  return Object.keys(e)
    .filter((k) => k !== "type" && k !== "stage" && k !== "state")
    .map((k) => `${k}=${e[k]}`)
    .join(" ");
}

function renderCounters(e) {
  const card = el(`stage-${e.stage}`);
  if (!card) {
    return;
  }
  const counters = card.querySelector(".counters");
  if (counters) {
    counters.textContent = counterText(e);
  }
}

function logLine(text, cls) {
  const log = el("log");
  if (!log) {
    return;
  }
  const line = document.createElement("div");
  line.className = "line" + (cls ? " " + cls : "");
  line.textContent = text;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function showBanner(message) {
  const banner = el("banner");
  if (banner) {
    banner.textContent = "Run failed: " + message;
    banner.classList.add("show");
  }
}

function hideBanner() {
  const banner = el("banner");
  if (banner) {
    banner.classList.remove("show");
  }
}

function setBusy(busy) {
  const btn = el("run-btn");
  if (btn) {
    btn.disabled = busy;
    btn.textContent = busy ? "Running…" : "Run pipeline";
  }
}

function resetRail() {
  for (const stage of RAIL) {
    const card = el(`stage-${stage}`);
    if (card) {
      card.removeAttribute("data-state");
      const counters = card.querySelector(".counters");
      if (counters) {
        counters.textContent = "";
      }
    }
  }
}

function finishRun() {
  setBusy(false);
}

// --- form wiring ----------------------------------------------------------
function gatherSections() {
  // The section matrix lands in items 18/19; the dashboard sends an empty
  // matrix (pack defaults apply). Kept as a seam so the wizard can populate it.
  return {};
}

async function populateHeader() {
  if (!hasApi()) {
    el("no-api").classList.add("show");
    setBusy(true); // no controller to run against; keep the button inert
    return;
  }
  try {
    const info = await window.pywebview.api.info();
    if (info && info.ok) {
      el("version").textContent = info.version;
      const select = el("pack");
      select.innerHTML = "";
      for (const pack of info.packs) {
        if (!pack.available) {
          continue;
        }
        const opt = document.createElement("option");
        opt.value = pack.name;
        opt.textContent = pack.name;
        select.appendChild(opt);
      }
    }
  } catch (err) {
    showBanner(String(err));
  }
}

async function onRun() {
  if (!hasApi()) {
    return;
  }
  hideBanner();
  resetRail();
  setBusy(true);
  const payload = {
    export_dir: el("export-dir").value,
    out_dir: el("out-dir").value,
    pack: el("pack").value,
    source: null,
    sections: gatherSections(),
    qa: el("qa").checked,
    archive: el("archive").checked,
    bundle: el("bundle").checked,
    ccda: el("ccda").checked,
  };
  try {
    // Fire-and-forget on a worker thread; results stream back via anastEvent.
    const started = await window.pywebview.api.run_pipeline_async(
      payload.export_dir,
      payload.out_dir,
      payload.pack,
      payload.source,
      payload.sections,
      payload.qa,
      payload.archive,
      payload.bundle,
      payload.ccda
    );
    if (started && started.ok === false) {
      showBanner(started.error);
      setBusy(false);
    }
  } catch (err) {
    showBanner(String(err));
    setBusy(false);
  }
}

function init() {
  const btn = el("run-btn");
  if (btn) {
    btn.addEventListener("click", onRun);
  }
  populateHeader();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
