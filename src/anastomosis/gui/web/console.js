/*
 * Anastomosis GUI — the Uploads view: watch charts being filed, start and stop.
 *
 * Owns the "upload" flow. Two jobs: read the record of what has been filed
 * (counts, plain-English states, the run calendar) and drive the resumable
 * filing engine through the controller.
 *
 * SECURITY: driving goes through upload_start / upload_stop ONLY. This file
 * never touches the record's write surface — every write (enqueue, state
 * changes, run bookkeeping) is owned by the controller. The browser connection
 * is loopback-only (hard-gated on the Python side) and the browser is never
 * closed by the GUI. While a run is in flight the live counts come from POLLING
 * upload_status: the events carry no counts, only stage/state names and, on a
 * failure, the exception TYPE.
 *
 * PHI discipline: every value rendered here is a count, a state name, a run or
 * destination id, an ISO timestamp, an exception TYPE name, or an opaque visit
 * key. Never a patient name, never a file path Anastomosis chose.
 */
"use strict";

(function () {
  const Shell = window.AnastShell;
  const el = (id) => document.getElementById(id);

  // The shared per-item retry budget, and the filename the engine gives the
  // record it writes beside the charts. These FALLBACKS are only for the
  // api-less browser preview; the live values are refreshed from the
  // Python-canonical gui_config() endpoint, and
  // tests/unit/test_frontend_constants.py pins each fallback to its Python
  // constant so neither side can drift alone.
  let DEFAULT_MAX_ATTEMPTS = 3;
  let LEDGER_NAME = "upload_ledger.sqlite";

  const POLL_INTERVAL_MS = 1500;
  let POLL_TIMER = null;
  let ITEM_KEYS = [];
  //: How many finished charts are ready to be filed (from the results folder),
  //: or null before anything has been counted.
  let READY_TO_FILE = null;
  const CAL = { year: null, month: null, histogram: {} };

  // Every filing state, in the operator's language, with the bucket it counts
  // toward. The technical id rides each row's tooltip (COPY_MAP). "Waiting"
  // means no filing was attempted — which is true of a skipped chart too.
  const STATE_INFO = {
    pending: { label: "Waiting to be filed", bucket: "waiting" },
    skipped_skiplist: { label: "Skipped at your request", bucket: "waiting" },
    resolving_patient: { label: "Finding the patient", bucket: "progress" },
    verifying_pre: { label: "Checking the patient before filing", bucket: "progress" },
    uploading: { label: "Filing the chart", bucket: "progress" },
    upload_interrupted: { label: "Interrupted — will resume", bucket: "progress" },
    retry_wait: { label: "Waiting to try again", bucket: "progress" },
    verifying_post: { label: "Checking the chart after filing", bucket: "progress" },
    completed: { label: "Filed and confirmed", bucket: "filed" },
    duplicate_at_destination: { label: "Already in the destination — left alone", bucket: "filed" },
    preflight_failed: { label: "Could not start — the chart failed its checks", bucket: "attention" },
    patient_not_found: { label: "Patient not found in the destination", bucket: "attention" },
    pre_verify_failed: { label: "Stopped before filing — the identity check did not pass", bucket: "attention" },
    post_verify_failed: { label: "Filed, but the after-check did not pass — needs review", bucket: "attention" },
    failed: { label: "Could not file", bucket: "attention" },
  };

  // A state this build does not know is NEVER silently dropped: it lands in
  // "Needs attention" under its raw id, so an engine that gained a state shows
  // up as something to look at rather than vanishing from the counts.
  function stateInfo(state) {
    return STATE_INFO[state] || { label: String(state), bucket: "attention" };
  }

  function hasApi() {
    return Shell.hasApi();
  }

  async function loadGuiConfig() {
    if (!hasApi() || typeof window.pywebview.api.gui_config !== "function") return;
    try {
      const cfg = await window.pywebview.api.gui_config();
      if (!cfg || !cfg.ok) return;
      if (Number.isInteger(cfg.max_attempts) && cfg.max_attempts > 0) {
        DEFAULT_MAX_ATTEMPTS = cfg.max_attempts;
      }
      if (typeof cfg.ledger_name === "string" && cfg.ledger_name) {
        LEDGER_NAME = cfg.ledger_name;
      }
    } catch (_) {
      /* keep the fallback */
    }
  }

  // The record a run writes: always beside the charts, inside the results
  // folder the engine hardened. That is not configurable and must not become
  // so — the record carries visit ids and lives inside the 0700 directory
  // deliberately.
  function runRecordPath(outDir) {
    if (!outDir) return "";
    const sep = outDir.includes("\\") && !outDir.includes("/") ? "\\" : "/";
    return outDir.replace(/[/\\]+$/, "") + sep + LEDGER_NAME;
  }

  // The record this VIEW is reading. Normally the one beside the charts; the
  // Advanced override points the viewer at a record kept somewhere else, which
  // is a reading affordance only — it cannot move where a run writes.
  function recordPath() {
    const elsewhere = el("uploads-record").value.trim();
    return elsewhere || runRecordPath(el("uploads-results-dir").value.trim());
  }

  // While a run is in flight the counters follow the record THAT RUN writes,
  // pinned when it started. Two reasons, both of them "the numbers on screen
  // must describe this run": an override left in the field would have the
  // counters reporting some other record's progress, and re-reading the field
  // every tick let an edit mid-run silently move which record they follow.
  let ACTIVE_RECORD = "";

  // --- reading the record ---------------------------------------------------
  function renderCounts(status) {
    const counts = status.counts || {};
    const buckets = { filed: 0, attention: 0, progress: 0, waiting: 0 };
    for (const [state, n] of Object.entries(counts)) {
      buckets[stateInfo(state).bucket] += n;
    }
    // A zero is not a state worth colouring, whatever bucket it belongs to:
    // "0 needs attention" in oxblood reads as an alarm about nothing.
    for (const [bucket, n] of Object.entries(buckets)) {
      const value = el(`uploads-count-${bucket}`);
      value.textContent = String(n);
      value.parentNode.dataset.zero = String(n === 0);
    }

    const grid = el("uploads-states");
    grid.innerHTML = "";
    const states = Object.keys(counts).sort();
    Shell.setEmpty("uploads-states", states.length === 0);
    if (states.length === 0) return;
    // Sorted by urgency, so what needs a person is at the top of the list
    // rather than wherever the alphabet put it.
    const order = { attention: 0, progress: 1, waiting: 2, filed: 3 };
    states.sort((a, b) => {
      const gap = order[stateInfo(a).bucket] - order[stateInfo(b).bucket];
      return gap !== 0 ? gap : a.localeCompare(b);
    });
    for (const state of states) {
      const info = stateInfo(state);
      const row = Shell.resultRow(info.bucket, [
        { text: info.label },
        { text: String(counts[state]), className: "result-n" },
      ]);
      row.title = state; // the technical id, on the tooltip
      grid.appendChild(row);
    }
  }

  function metaValue(host, value, label, signal) {
    const wrap = document.createElement("div");
    wrap.className = "value value--sm";
    if (signal) wrap.dataset.signal = signal;
    wrap.dataset.zero = String(value === "0");
    const n = document.createElement("span");
    n.className = "value-n";
    n.textContent = value;
    const k = document.createElement("span");
    k.className = "value-k";
    k.textContent = label;
    wrap.appendChild(n);
    wrap.appendChild(k);
    host.appendChild(wrap);
  }

  function renderMeta(status) {
    const meta = el("uploads-meta");
    meta.innerHTML = "";
    const kinds = Object.keys(status.error_type_histogram || {}).length;
    metaValue(meta, String(status.total || 0), "Charts recorded");
    if (READY_TO_FILE !== null) metaValue(meta, String(READY_TO_FILE), "Ready to file");
    metaValue(meta, String(kinds), "Kinds of error", kinds > 0 ? "attention" : null);

    // When the run happened is a sentence, not a value: a timestamp read as a
    // 34px numeral is unreadable, and there is nothing to compare it against.
    const when = el("uploads-when");
    const run = status.run;
    if (!run) {
      when.textContent = "No filing run has been recorded from this folder.";
      return;
    }
    const finished = run.finished_at ? `finished ${run.finished_at}` : "still running";
    when.textContent = run.aborted_reason
      ? `Started ${run.started_at}, ${finished} — stopped early: ${run.aborted_reason}.`
      : `Started ${run.started_at}, ${finished}.`;
  }

  function renderErrorKinds(hist) {
    const box = el("uploads-kinds-body");
    box.innerHTML = "";
    const kinds = Object.keys(hist).sort();
    if (kinds.length === 0) {
      box.textContent = "Nothing has gone wrong in this run.";
      return;
    }
    for (const kind of kinds) {
      const row = document.createElement("div");
      row.className = "kind-row";
      const name = document.createElement("span");
      name.textContent = kind;
      const count = document.createElement("span");
      count.className = "kind-count";
      count.textContent = String(hist[kind]);
      row.appendChild(name);
      row.appendChild(count);
      box.appendChild(row);
    }
  }

  // PHI-safe by construction: the only data plotted is the run's own ISO
  // timestamps and one count per active day.
  function buildCalendar(run) {
    CAL.histogram = {};
    if (!run || !run.started_at) {
      const now = new Date();
      CAL.year = now.getFullYear();
      CAL.month = now.getMonth();
      drawCalendar();
      return;
    }
    const started = String(run.started_at).slice(0, 10);
    const [y, m] = started.split("-").map((s) => parseInt(s, 10));
    CAL.year = y;
    CAL.month = m - 1;
    // ONE run, counted once, on the day it started.
    //
    // The finish day used to add a second tally, so a run that started and
    // finished the same day showed a badge of "2" — under a legend whose green
    // dot reads "Filed", next to counters correctly reading "57 Filed". The
    // badge counts RUNS; nothing about it was ever a chart count.
    CAL.histogram[started] = run.aborted_reason
      ? { pending: 0, done: 0, errors: 1 }
      : { pending: 0, done: 1, errors: 0 };
    drawCalendar();
  }

  function drawCalendar() {
    Shell.renderCalendar({
      gridEl: el("uploads-cal-grid"),
      titleEl: el("uploads-cal-title"),
      year: CAL.year,
      month: CAL.month,
      histogram: CAL.histogram,
    });
  }

  function navigateMonth(delta) {
    if (CAL.year === null) {
      const now = new Date();
      CAL.year = now.getFullYear();
      CAL.month = now.getMonth();
    }
    CAL.month += delta;
    while (CAL.month < 0) {
      CAL.month += 12;
      CAL.year -= 1;
    }
    while (CAL.month > 11) {
      CAL.month -= 12;
      CAL.year += 1;
    }
    drawCalendar();
  }

  async function onRefresh(opts) {
    if (!hasApi()) return;
    const quiet = !!(opts && opts.quiet);
    const record = recordPath();
    if (!record) {
      if (!quiet) {
        Shell.showBanner(
          "Fill in the results folder first — the record of uploads lives beside the charts."
        );
      }
      return;
    }
    // Count the finished charts BEFORE reading the record, so the one meta row
    // is rendered once, complete.
    const outDir = el("uploads-results-dir").value.trim();
    if (outDir) {
      try {
        const preview = await window.pywebview.api.upload_manifest_preview(outDir);
        if (preview && preview.ok) READY_TO_FILE = preview.renderable;
      } catch (_) {
        /* the preview is advisory; never block the view on it */
      }
    }
    try {
      const status = await window.pywebview.api.upload_status(record);
      if (!status || !status.ok) {
        // Arriving at a folder that has never been filed from is the ordinary
        // first case, not a failure worth a banner.
        if (!quiet) {
          Shell.showBanner(
            `The record of uploads could not be read: ${status ? status.error : "no answer from the app"}`
          );
        }
        return;
      }
      renderCounts(status);
      renderMeta(status);
      renderErrorKinds(status.error_type_histogram || {});
      buildCalendar(status.run);
      el("uploads-counters").hidden = false;
      el("uploads-calendar").hidden = false;
      // A read the operator asked for is worth a line; one that happened
      // because they arrived is not, and it would push the handoff note the
      // migration just wrote off the strip.
      if (!quiet) {
        Shell.logEvent({ kind: "ok", msg: `Uploads: ${status.total || 0} charts in the record.` });
      }
    } catch (err) {
      Shell.showBanner(String(err));
    }
    await refreshSearch();
  }

  // Quiet re-read while a run is in flight (counts come from the record, never
  // from an event). A transient read failure must not break the run.
  async function refreshQuietly() {
    if (!hasApi()) return;
    const record = ACTIVE_RECORD || recordPath();
    if (!record) return;
    try {
      const status = await window.pywebview.api.upload_status(record);
      if (status && status.ok) {
        renderCounts(status);
        renderMeta(status);
        renderErrorKinds(status.error_type_histogram || {});
        el("uploads-counters").hidden = false;
      }
    } catch (_) {
      /* advisory */
    }
  }

  function startPolling() {
    stopPolling();
    POLL_TIMER = window.setInterval(refreshQuietly, POLL_INTERVAL_MS);
  }
  function stopPolling() {
    if (POLL_TIMER !== null) {
      window.clearInterval(POLL_TIMER);
      POLL_TIMER = null;
    }
  }

  // --- the visible search over visit ids ------------------------------------
  // This replaces the hidden ⌘K palette: same filtering, discoverable.
  async function refreshSearch() {
    if (!hasApi()) {
      ITEM_KEYS = [];
      return;
    }
    const record = recordPath();
    if (!record) {
      ITEM_KEYS = [];
      return;
    }
    try {
      const res = await window.pywebview.api.upload_item_keys(record);
      ITEM_KEYS = res && res.ok ? res.item_keys : [];
    } catch (_) {
      ITEM_KEYS = [];
    }
    renderSearch();
  }

  function renderSearch() {
    const host = el("uploads-search-results");
    const query = el("uploads-search").value.trim().toLowerCase();
    host.innerHTML = "";
    if (ITEM_KEYS.length === 0) {
      const empty = document.createElement("div");
      empty.className = "search-empty";
      empty.textContent = "Nothing is waiting to be filed.";
      host.appendChild(empty);
      return;
    }
    const matches = query ? ITEM_KEYS.filter((key) => key.toLowerCase().includes(query)) : ITEM_KEYS;
    if (matches.length === 0) {
      const empty = document.createElement("div");
      empty.className = "search-empty";
      empty.textContent = "No upload matches that.";
      host.appendChild(empty);
      return;
    }
    for (const key of matches.slice(0, 50)) {
      const row = document.createElement("div");
      row.className = "search-result";
      row.textContent = key; // a visit id plus a content fingerprint — never a name
      host.appendChild(row);
    }
  }

  // --- driving --------------------------------------------------------------
  async function loadSafetyNotice() {
    if (!hasApi()) return;
    try {
      const res = await window.pywebview.api.upload_safety_notice();
      if (res && res.ok) el("uploads-safety").textContent = res.warning;
    } catch (_) {
      /* advisory */
    }
  }

  async function onStart() {
    if (!hasApi()) return;
    const outDir = el("uploads-results-dir").value.trim();
    const browser = el("uploads-browser").value.trim();
    const assistant = el("uploads-assistant").value.trim();
    if (!outDir || !browser || !assistant) {
      Shell.showBanner("Fill in the results folder, the filing assistant, and the browser connection.");
      return;
    }
    const folder = el("uploads-assistant-folder").value.trim();
    // Optional skip list: one visit id per line; the controller drops blanks
    // and "#" notes. An empty box means no skip list at all.
    const skiplist = el("uploads-skiplist")
      .value.split("\n")
      .map((line) => line.trim())
      .filter((line) => line.length > 0);
    const verify = !!el("uploads-verify").checked;
    try {
      const res = await window.pywebview.api.upload_start(
        outDir,
        browser,
        assistant,
        folder ? [folder] : null,
        skiplist.length ? skiplist : null,
        DEFAULT_MAX_ATTEMPTS,
        verify
      );
      if (!res || !res.ok) {
        // Through the shared table, like every other view. This one printed
        // the raw sentinel.
        Shell.showBanner(
          Shell.refusalText(
            res ? res.error : "no answer from the app",
            "Filing could not start"
          )
        );
        Shell.logEvent({ kind: "error", msg: "Uploads: filing did not start." });
        return;
      }
      Shell.logEvent({ kind: "ok", msg: "Uploads: filing started." });
      ACTIVE_RECORD = runRecordPath(outDir);
      startPolling();
    } catch (err) {
      Shell.showBanner(String(err));
    }
  }

  async function onStop() {
    if (!hasApi()) return;
    try {
      const res = await window.pywebview.api.upload_stop();
      Shell.logEvent({
        kind: "info",
        msg: res && res.ok ? "Uploads: stopping after the current chart." : "Uploads: nothing is running.",
      });
    } catch (err) {
      Shell.showBanner(String(err));
    }
  }

  function onTerminal() {
    stopPolling();
    refreshQuietly();
    ACTIVE_RECORD = "";
  }

  function onEvent(event) {
    if (event.type === "stage" && event.stage === "upload" && event.state === "done") {
      onTerminal();
    } else if (event.type === "error" && event.stage === "upload") {
      onTerminal();
      Shell.showBanner(`Filing stopped: ${event.error}`);
    }
  }

  // The Migrate handoff: the destination's filing assistant and the folder the
  // charts were written to, so nothing is typed twice.
  //
  // Only ever what this arrival was handed. `onEnter` runs on EVERY arrival at
  // this view, and the handoff also used to live in a shell-global that was
  // never cleared — so an operator who retargeted these two fields by hand for
  // a second batch, looked at another view and came back had them silently
  // reverted to the migration's, and "Start filing" then drove the wrong folder
  // into the wrong destination. Overwriting a field the operator can see is
  // also worth a line in the strip.
  function onEnter(handoff) {
    // The record is what this view is FOR, so arriving reads it. It used to
    // take a click on "Show what has been filed", which meant the screen an
    // operator came to after a migration was a form and three hidden panels.
    // Advisory: a folder with no record simply leaves the empty states up.
    if (!handoff) {
      void onRefresh({ quiet: true });
      return;
    }
    const changed = [];
    if (handoff.assistant && el("uploads-assistant").value !== handoff.assistant) {
      el("uploads-assistant").value = handoff.assistant;
      changed.push("filing assistant");
    }
    if (handoff.outDir && el("uploads-results-dir").value !== handoff.outDir) {
      el("uploads-results-dir").value = handoff.outDir;
      changed.push("results folder");
    }
    if (changed.length) {
      Shell.logEvent({
        kind: "ok",
        msg: `Uploads: ${changed.join(" and ")} taken from the migration you just ran.`,
      });
    }
    void onRefresh({ quiet: true });
  }

  function init() {
    el("uploads-refresh").addEventListener("click", () => onRefresh());
    el("uploads-start").addEventListener("click", onStart);
    el("uploads-stop").addEventListener("click", onStop);
    el("uploads-search").addEventListener("input", renderSearch);
    el("uploads-cal-prev").addEventListener("click", () => navigateMonth(-1));
    el("uploads-cal-next").addEventListener("click", () => navigateMonth(1));
    el("uploads-kinds-btn").addEventListener("click", () => {
      el("uploads-kinds").classList.toggle("show");
    });

    Shell.onReady((live) => {
      const start = el("uploads-start");
      start.disabled = !live;
      el("uploads-stop").disabled = !live;
      if (!live) return;
      loadGuiConfig();
      loadSafetyNotice();
    });
  }

  Shell.registerView({
    name: "uploads",
    title: "Uploads",
    flow: "upload",
    onEvent,
    onEnter,
    // No onLeave: polling deliberately survives a view switch, so a run started
    // here keeps its counts current for whenever the operator comes back.
  });
  init();
})();
