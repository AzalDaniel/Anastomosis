/*
 * Anastomosis GUI — the shell: router, bridge, event bus, shared components.
 *
 * ONE document, four views (DESIGN_LANGUAGE §7/§9). Everything that used to be
 * duplicated across five pages lives here exactly once: the pywebview bridge
 * bootstrap, the `window.anastEvent` dispatcher, the activity strip, the icon
 * set, the gooey segment toggle, the calendar, and the run form that Charts and
 * Migrate both instantiate.
 *
 * Event routing: every controller event carries a `flow` naming the operation
 * family that raised it. The dispatcher paints the GLOBAL activity strip for
 * every event and then hands it to the ONE view that registered that flow — so
 * a run keeps reporting from whichever view is on screen, and no view can
 * consume another's terminal event.
 *
 * PHI discipline: nothing here ships a value. Events carry counts, stage names,
 * ids and exception TYPE names; the per-patient roll-up is fetched over the
 * bridge and painted with textContent for local display only, never logged.
 */
"use strict";

(function () {
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const el = (id) => document.getElementById(id);
  const prefersReduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ─── Icons ────────────────────────────────────────────────────
  // One set: 20×20, stroke currentColor, 1.5, no fills (§8). Replaces the
  // ✓ ⚠ ✗ · glyph constants; no emoji anywhere, ever.
  const ICON_PATHS = {
    ok: ["M4 10.5l4 4 8-9"],
    warn: ["M10 3.5 2.8 16.5h14.4L10 3.5Z", "M10 8.5v3.2", "M10 14.2h.01"],
    error: ["M6 6l8 8", "M14 6l-8 8"],
    info: ["M10 9v5", "M10 6h.01", "M10 17.5a7.5 7.5 0 1 0 0-15 7.5 7.5 0 0 0 0 15Z"],
    waiting: ["M10 17.5a7.5 7.5 0 1 0 0-15 7.5 7.5 0 0 0 0 15Z", "M10 6v4.2l2.8 1.7"],
    search: ["M9 15.5a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13Z", "M17.5 17.5l-3.9-3.9"],
    close: ["M5 5l10 10", "M15 5L5 15"],
    "chevron-left": ["M12.5 4L6.5 10l6 6"],
    "chevron-right": ["M7.5 4l6 6-6 6"],
  };

  // Assembled from the table above and parsed as markup — the only innerHTML in
  // the app that is not a value, and the strings are entirely internal (a name
  // this set does not carry falls back to `info`; nothing from a controller,
  // a file, or a person ever reaches here). Parsing rather than
  // createElementNS also keeps the bundled assets free of any URL, including
  // the SVG namespace one, which the offline scan greps for.
  function iconMarkup(name) {
    const paths = (ICON_PATHS[name] || ICON_PATHS.info)
      .map((d) => `<path d="${d}" />`)
      .join("");
    return (
      '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" ' +
      `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`
    );
  }

  function icon(name) {
    const host = document.createElement("span");
    host.innerHTML = iconMarkup(name);
    return host.firstElementChild;
  }

  // Fill every <span class="icon" data-icon="…"> in `root` from the set above,
  // so the markup never carries a second copy of the path data.
  function paintIcons(root) {
    for (const host of $$("[data-icon]", root || document)) {
      host.innerHTML = iconMarkup(host.dataset.icon);
    }
  }

  // ─── The bridge ───────────────────────────────────────────────
  function hasApi() {
    return typeof window.pywebview !== "undefined" && !!window.pywebview.api;
  }

  const READY = [];
  //: Callbacks run once now and once more if pywebview attaches late. Each gets
  //: `live` (whether the bridge answered) and must be idempotent.
  function onReady(cb) {
    READY.push(cb);
  }

  let VERSION = "";

  // info() answers once for the whole app: the version for About, and the
  // source/layout lists every run form needs.
  let INFO = null;
  const INFO_CBS = [];
  function onInfo(cb) {
    INFO_CBS.push(cb);
    if (INFO) cb(INFO);
  }
  async function loadInfo() {
    if (!hasApi()) return;
    try {
      const info = await window.pywebview.api.info();
      if (!info || !info.ok) return;
      INFO = info;
      VERSION = String(info.version || "");
      const line = el("about-version");
      if (line) {
        // The attribute is the machine-readable proof the bridge round-tripped;
        // an empty one means info() never answered.
        line.dataset.version = VERSION;
        line.textContent = `Anastomosis ${VERSION} · AGPL-3.0`;
      }
      for (const cb of INFO_CBS) cb(info);
    } catch (err) {
      showBanner(String(err));
    }
  }

  // ─── Views and the router ─────────────────────────────────────
  const VIEWS = {};
  const BY_FLOW = {};
  let CURRENT = "charts";

  //: A view registers its section name, the event flow it owns, and optional
  //: hooks: onEvent(event), onEnter(context), onLeave().
  function registerView(spec) {
    VIEWS[spec.name] = spec;
    if (spec.flow) BY_FLOW[spec.flow] = spec;
  }

  //: A second flow for a view that hosts two of them (Teach: a document layout
  //: and an export format are separate controller flows in one workspace).
  function registerFlow(flow, onEvent) {
    BY_FLOW[flow] = { onEvent };
  }

  function section(name) {
    return $(`[data-view="${name}"]`);
  }

  // Crossfade: opacity + 2px of travel, 240ms, `hidden` toggled at the
  // boundaries so an inactive view costs no layout. Reduced motion zeroes it.
  function showView(name, context) {
    const incoming = section(name);
    if (!incoming) return;
    const spec = VIEWS[name];
    if (name === CURRENT) {
      if (spec && spec.onEnter) spec.onEnter(context || null);
      return;
    }
    const outgoing = section(CURRENT);
    const leaving = VIEWS[CURRENT];
    CURRENT = name;
    // A banner belongs to the screen that raised it. It used to be cleared only
    // by the five run/analyze entry points — Uploads never cleared it and no
    // view switch did — so an operator who read it, fixed the problem and moved
    // on still had the red bar following them around.
    hideBanner();

    // The pill owns the selected state. Told, not asked: a view can also be
    // reached from a button inside another view (Migrate's "Continue on
    // Uploads"), and the pill has to follow that too.
    selectSegment("view", name);

    const swap = () => {
      if (outgoing) {
        outgoing.hidden = true;
        outgoing.classList.remove("view--leaving");
      }
      if (leaving && leaving.onLeave) leaving.onLeave();
      incoming.hidden = false;
      incoming.classList.add("view--entering");
      if (spec && spec.onEnter) spec.onEnter(context || null);
      // Two frames: the first commits the entering state, the second animates
      // out of it. One frame is not enough — the browser would coalesce both.
      requestAnimationFrame(() =>
        requestAnimationFrame(() => incoming.classList.remove("view--entering"))
      );
    };

    if (prefersReduced()) {
      swap();
    } else {
      if (outgoing) outgoing.classList.add("view--leaving");
      window.setTimeout(swap, 240);
    }
    // Deliberately NOT logged: the strip belongs to the runs, and a view switch
    // must never overwrite the last thing a run said.
  }

  // ─── The event dispatcher (one, for every flow) ───────────────
  const FLOW_LABEL = {
    pipeline: "Charts",
    migration: "Migrate",
    upload: "Uploads",
    pack_init: "Teach",
    source_init: "Teach",
    query: "Anastomosis",
  };
  const STAGE_LABEL = {
    ingest: "Reading records",
    reconstruct: "Building charts",
    qa: "Double-checking",
    deliver: "Saving results",
    upload: "Filing charts",
    packgen: "Reading the samples",
    source: "Reading the example",
  };
  function stageLabel(stage) {
    return STAGE_LABEL[stage] || String(stage || "");
  }

  // Envelope keys that are not counts: the discriminators plus the run's opaque
  // summary handle (noise in an operator's activity list).
  // `not_carried`'s bare key name says nothing: it is QA's "N fact(s) carried
  // by the record summary, not the visit charts" register, spelled out here in
  // the CLI's own words (pipeline.py's settle_qa, #297). settle_qa puts the key
  // on the counts dict only when it is nonzero, so this reads exactly the CLI's
  // "only when there is something to say" rule — silent whenever a chart
  // abbreviates nothing. Kept in step with app.js's copy: the activity strip
  // and the Charts rail read the SAME event, and must say the same thing.
  const COUNT_TEXT = {
    not_carried: (n) => `${n} fact(s) carried by the record summary, not the visit charts`,
  };
  const NON_COUNT_KEYS = [
    "type",
    "stage",
    "state",
    "flow",
    "summary_id",
    "notice",
    "outcome",
    "source_reading",
  ];
  function countsText(event) {
    return Object.keys(event)
      .filter((k) => !NON_COUNT_KEYS.includes(k))
      .map((k) => (COUNT_TEXT[k] ? COUNT_TEXT[k](event[k]) : `${k.replace(/_/g, " ")} ${event[k]}`))
      .join(" · ");
  }

  function describe(event) {
    const who = FLOW_LABEL[event.flow] || String(event.flow || "");
    switch (event.type) {
      case "stage":
        return {
          kind: "info",
          msg: `${who}: ${stageLabel(event.stage)}${event.state === "done" ? " — done" : "…"}`,
        };
      case "progress": {
        const counts = countsText(event);
        return {
          kind: "info",
          msg: `${who}: ${stageLabel(event.stage)}${counts ? ` · ${counts}` : ""}`,
        };
      }
      case "done": {
        const counts = countsText(event);
        return { kind: "ok", msg: `${who}: finished${counts ? ` · ${counts}` : ""}` };
      }
      case "error":
        return { kind: "error", msg: `${who}: stopped — ${event.error}` };
      default:
        return null;
    }
  }

  // The ONE dispatcher the Python sink calls. Paints the shared strip for every
  // event, then routes to the view that owns the flow.
  window.anastEvent = function anastEvent(event) {
    if (!event || typeof event !== "object") return;
    const line = describe(event);
    if (line) logEvent(line);
    const owner = BY_FLOW[event.flow];
    if (owner && owner.onEvent) owner.onEvent(event);
  };

  // ─── Activity strip + drawer ──────────────────────────────────
  const MAX_LOG_ROWS = 200;
  const KIND_ICON = { ok: "ok", warn: "warn", error: "error", info: "info" };

  function fmtTime(d) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  // entry: {kind, msg, quiet}. `quiet` entries (view switches) update the strip
  // but are not worth a history row. The message is whatever PHI-free text the
  // caller built — stage names, counts, exception type names.
  function logEvent(entry) {
    const kind = entry.kind || "info";
    const msg = entry.msg == null ? "" : String(entry.msg);
    const ts = fmtTime(new Date());

    const strip = el("log-strip");
    if (strip) {
      strip.dataset.kind = kind;
      const stripTs = el("log-strip-ts");
      if (stripTs) stripTs.textContent = ts;
      const stripIcon = el("log-strip-icon");
      if (stripIcon) {
        stripIcon.textContent = "";
        stripIcon.appendChild(icon(KIND_ICON[kind] || "info"));
      }
      const stripMsg = el("log-strip-msg");
      if (stripMsg) stripMsg.textContent = msg;
    }
    if (entry.quiet) return;

    const rows = el("log-rows");
    if (!rows) return;
    const row = document.createElement("div");
    row.className = `log-row log-row--${kind}`;
    const tsEl = document.createElement("span");
    tsEl.className = "log-ts";
    tsEl.textContent = ts;
    const iconEl = document.createElement("span");
    iconEl.className = "icon log-icon";
    iconEl.appendChild(icon(KIND_ICON[kind] || "info"));
    const msgEl = document.createElement("span");
    msgEl.className = "log-msg";
    msgEl.textContent = msg;
    row.appendChild(tsEl);
    row.appendChild(iconEl);
    row.appendChild(msgEl);
    rows.appendChild(row);
    while (rows.childElementCount > MAX_LOG_ROWS) rows.removeChild(rows.firstChild);
    const drawer = el("log-drawer");
    if (drawer && !drawer.hidden) rows.scrollTop = rows.scrollHeight;
  }

  // ─── Disclosures (a button, and the thing it opens) ──────────
  //
  // Three of these — About, the activity drawer, the error-kinds flyout — and
  // each was written separately and forgot something different. About had
  // Escape; the flyout had neither Escape nor `aria-expanded`, so nothing said
  // it was open. The drawer was a `role="dialog"` that focus never entered and
  // Escape never closed, which leaves a keyboard operator opening a panel they
  // then have to Tab through the whole page to reach.
  //
  // This is the one implementation, and what it owns is exactly the part all
  // three got wrong: the trigger says whether it is open, Escape closes it, a
  // click elsewhere closes it, and focus goes in and comes back.
  //
  // Showing and hiding stay with the caller. The drawer scrolls its rows to
  // the bottom on the way in and the flyout slides on a class rather than
  // `hidden`; neither is this function's business.
  const FOCUSABLE =
    'button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

  function initDisclosure(opts) {
    const trigger = opts.trigger;
    const panel = opts.panel;
    if (!trigger || !panel) return null;
    const isOpen = opts.isOpen;

    const open = () => {
      opts.show();
      trigger.setAttribute("aria-expanded", "true");
      // Somewhere inside, or the panel itself. A dialog you cannot reach is
      // the same as a dialog that did not open.
      const first = panel.querySelector(FOCUSABLE);
      if (first) {
        first.focus();
      } else {
        panel.tabIndex = -1;
        panel.focus();
      }
    };
    // `restore` is false for a click elsewhere, and that is a statement of
    // intent rather than a mechanism: the browser's own mousedown handling
    // focuses whatever was clicked — or the body, if nothing there takes
    // focus — after this listener returns, so a trigger.focus() here would be
    // overwritten a moment later either way. Asking for something that cannot
    // happen is a worse thing to leave in a file than not asking.
    const close = (restore) => {
      if (!isOpen()) return;
      opts.hide();
      trigger.setAttribute("aria-expanded", "false");
      if (restore) trigger.focus();
    };
    const toggle = () => (isOpen() ? close(true) : open());

    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      toggle();
    });
    document.addEventListener("mousedown", (e) => {
      if (!isOpen() || panel.contains(e.target) || trigger.contains(e.target)) return;
      close(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") close(true);
    });
    return { open, close, toggle, isOpen };
  }

  //: Where a letter means the letter and not a shortcut: somewhere that takes
  //: typing by its nature. A chooser trigger is a <button> and also takes
  //: typing, but it says so itself by calling preventDefault() on the character
  //: — the `defaultPrevented` check below is the general form of that, and
  //: naming the chooser here as well would be a second rule saying the same
  //: thing, free to disagree with the first.
  function takesTyping(node) {
    if (!node) return false;
    if (node.isContentEditable) return true;
    const tag = node.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
  }

  let logDrawer = null;
  function initLogDrawer() {
    const drawer = el("log-drawer");
    const strip = el("log-strip");
    if (!drawer || !strip) return;
    logDrawer = initDisclosure({
      trigger: strip,
      panel: drawer,
      isOpen: () => !drawer.hidden,
      show: () => {
        drawer.hidden = false;
        const rows = el("log-rows");
        if (rows) rows.scrollTop = rows.scrollHeight;
      },
      hide: () => {
        drawer.hidden = true;
      },
    });
  }

  // ─── Banner ───────────────────────────────────────────────────
  // The controller answers a submit it cannot take with a short machine-readable
  // sentinel, not a message — `Busy` is pinned by name across the controller
  // tests and has to stay a sentinel. This is the one place it becomes something
  // a physician can act on. Anything unrecognised is passed through as-is; a
  // `prefix` is only used on those, because "the samples could not be read:
  // Busy" describes a failure that never happened.
  // Each refusal the controller can hand back, in words that say what to DO.
  // Uploads bypassed this table entirely and printed the raw code — so the
  // screen that files charts into a live EHR answered "Filing could not start:
  // BadCdpEndpoint", which tells an operator nothing, while every other view
  // rendered the sentence below for the same class of refusal.
  const REFUSALS = {
    Busy: "Anastomosis is already working on something else. Wait for that to finish, then try again.",
    BadCdpEndpoint:
      "That browser connection is not on this computer. Filing only drives a browser " +
      "running locally — check the address in Advanced.",
    BadManifest:
      "No filing list was found in that results folder. Rebuild the charts with " +
      "“Write the filing list” switched on, then try again.",
    PackNotReady:
      "The filing assistant for this system has not been set up on this computer yet. " +
      "Set it up, then come back.",
    NoRun: "Nothing is being filed at the moment.",
  };
  function refusalText(error, prefix) {
    const sentinel = String(error == null ? "" : error);
    if (sentinel in REFUSALS) return REFUSALS[sentinel];
    return prefix ? `${prefix}: ${sentinel}` : sentinel;
  }

  function showBanner(message) {
    const banner = el("banner");
    const text = el("banner-text");
    if (!banner || !text) return;
    text.textContent = String(message);
    banner.classList.add("show");
  }
  // Empties the box rather than removing it. `#banner` is `role="alert"` and
  // stays in the accessibility tree at all times (app.css clips it while it is
  // not `.show`), so the arrival of TEXT is what an assistive technology has to
  // notice — which only works if the text is what comes and goes.
  function hideBanner() {
    const banner = el("banner");
    const text = el("banner-text");
    if (!banner) return;
    banner.classList.remove("show");
    if (text) text.textContent = "";
  }

  // ─── Saying it out loud ───────────────────────────────────────
  //
  // Every view narrates a run in one line on screen — "Reading records…",
  // "Step 2 of 2", "Finished." — and until now that line was the ONLY feedback
  // a click produced and none of it was announced. A screen-reader operator
  // pressed the button and heard nothing at all, which is also why they pressed
  // it again.
  //
  // One announcer for the whole document, not a `role="status"` on each of
  // those five lines. Two of them (`#migrate-result`, `#uploads-counters`) are
  // toggled with `hidden`, and a live region that is revealed in the same task
  // as its first words is the case assistive technologies handle worst — the
  // region has to already be in the accessibility tree when the text lands.
  // A single always-present node has no such state to get wrong.
  //
  // Polite, never assertive: this is commentary alongside work the operator
  // asked for. The interrupting channel is `#banner` (`role="alert"`), for the
  // things that stopped.
  function announce(text) {
    const region = el("announcer");
    if (!region) return;
    const msg = String(text == null ? "" : text);
    // A live region announces a MUTATION, not a change of value: rewriting a
    // node with the words already in it says them again. Uploads re-reads the
    // record on a timer, so without this an idle run repeated itself forever.
    if (region.textContent === msg) return;
    region.textContent = msg;
  }

  // The visible line and the spoken one, written together from one string, so
  // they cannot drift apart. Nothing is said when the line did not change: the
  // views repaint their resting state on boot ("Ready.", "Step 1 of 2 — look at
  // the samples."), and an app that greets a screen-reader operator by reading
  // out the words already on the page is one they will switch off.
  function setStatus(node, text) {
    if (!node) return;
    const msg = String(text == null ? "" : text);
    if (node.textContent === msg) return;
    node.textContent = msg;
    announce(msg);
  }

  // ─── Segment toggles (the gooey pill) ─────────────────────────
  // Carried from the Tebra reference: click/arrow-keys snap with a scaleX(1.45)
  // stretch; pointer-down + drag follows the cursor and snaps to the nearest
  // slot on release. --segment-count/--segment-index are written through the
  // CSSOM because the strict `style-src 'self'` CSP refuses a markup style="".
  // One caller: the view nav. A sliding pill means "peer destinations", so it is
  // a tablist and nothing else in the app wears it — a binary setting is a
  // switch (see makeSwitchField) and one of N is a chooser.
  // Setting a toggle from outside, WITHOUT calling back: the view nav has to
  // follow a view change that started somewhere else (Migrate's "Continue on
  // Uploads"), and a notify here would bounce straight back into showView.
  const SEGMENTS = new Map();
  function selectSegment(name, value) {
    const set = SEGMENTS.get(name);
    if (set) set(value);
  }

  function initSegmentToggles(root, onChange) {
    $$(".segment-toggle", root).forEach((toggle) => {
      if (toggle.dataset.wired === "true") return;
      toggle.dataset.wired = "true";
      const options = $$(".segment-option", toggle);
      if (!options.length) return;
      const count = options.length;
      toggle.style.setProperty("--segment-count", String(count));

      const activate = (nextIdxRaw, opts) => {
        const animate = !opts || opts.animate !== false;
        const notify = !opts || opts.notify !== false;
        const nextIdx = Math.max(0, Math.min(count - 1, Math.round(nextIdxRaw)));
        const fromFloat = parseFloat(toggle.style.getPropertyValue("--segment-index") || "0");
        const fromIdx = Math.round(fromFloat);
        const opt = options[nextIdx];
        if (!opt) return;
        const sameSlot = nextIdx === fromIdx && Math.abs(fromFloat - fromIdx) < 0.001;
        if (animate && sameSlot) return;

        const changed = opt.dataset.value !== toggle.dataset.value;
        toggle.dataset.value = opt.dataset.value;
        if (animate && nextIdx !== fromIdx) {
          // Checked per activation, never cached: the operator can change the
          // system setting while the app is open.
          const settle = prefersReduced() ? "is-settling" : "is-stretching";
          toggle.style.setProperty("--segment-from", String(fromIdx));
          toggle.classList.remove("is-stretching", "is-settling");
          void toggle.offsetWidth; // force reflow so the restart registers
          toggle.classList.add(settle);
        }
        toggle.style.setProperty("--segment-index", String(nextIdx));
        options.forEach((o, i) => {
          const selected = i === nextIdx;
          o.setAttribute("aria-selected", selected ? "true" : "false");
          o.classList.toggle("is-live", selected);
          o.tabIndex = selected ? 0 : -1;
        });
        if (changed && notify && typeof onChange === "function") {
          onChange(toggle.dataset.name, opt.dataset.value);
        }
      };

      // Named toggles can also be set from outside — see `selectSegment`.
      if (toggle.dataset.name) {
        SEGMENTS.set(toggle.dataset.name, (value) => {
          const idx = options.findIndex((o) => o.dataset.value === value);
          if (idx >= 0) activate(idx, { notify: false });
        });
      }

      const drag = { active: false, pointerId: null, startX: 0, startIdx: 0, moved: false, slotWidth: 0, originX: 0 };
      const measure = () => {
        const rect = toggle.getBoundingClientRect();
        drag.slotWidth = (rect.width - 8) / count; // 8px = 2×4px padding
        drag.originX = rect.left + 4;
      };
      const xToIndex = (clientX) => {
        const idx = (clientX - drag.originX) / drag.slotWidth - 0.5;
        return Math.max(0, Math.min(count - 1, idx));
      };

      toggle.addEventListener("pointerdown", (e) => {
        if (e.button !== undefined && e.button !== 0) return;
        drag.active = true;
        drag.pointerId = e.pointerId;
        drag.moved = false;
        drag.startX = e.clientX;
        measure();
        drag.startIdx = parseFloat(toggle.style.getPropertyValue("--segment-index") || "0");
        try {
          toggle.setPointerCapture(e.pointerId);
        } catch (_) {
          /* capture is optional */
        }
      });
      toggle.addEventListener("pointermove", (e) => {
        if (!drag.active || e.pointerId !== drag.pointerId) return;
        if (!drag.moved && Math.abs(e.clientX - drag.startX) < 4) return; // 4px deadzone
        if (!drag.moved) {
          drag.moved = true;
          toggle.classList.add("is-dragging");
          toggle.classList.remove("is-stretching");
        }
        const live = xToIndex(e.clientX);
        toggle.style.setProperty("--segment-index", String(live));
        // The blob arrives before the commit does: light the label it is over,
        // while the SELECTION still names the option that is actually chosen.
        const under = Math.round(live);
        options.forEach((o, i) => o.classList.toggle("is-live", i === under));
        e.preventDefault();
      });
      const finishDrag = (e, opts) => {
        const canceled = opts && opts.canceled;
        if (!drag.active || (e && e.pointerId !== drag.pointerId)) return;
        const wasMoved = drag.moved;
        drag.active = false;
        drag.moved = false;
        try {
          toggle.releasePointerCapture(drag.pointerId);
        } catch (_) {
          /* release is optional */
        }
        drag.pointerId = null;
        toggle.classList.remove("is-dragging");
        if (!wasMoved) {
          // A plain tap resolves HERE: pointerdown took capture on the TOGGLE,
          // so the browser retargets the following click to the capture element
          // and the per-option click listener never fires for a real press.
          if (!canceled && e && typeof e.clientX === "number") activate(xToIndex(e.clientX));
          return;
        }
        const finalFloat = parseFloat(toggle.style.getPropertyValue("--segment-index") || "0");
        void toggle.offsetWidth;
        activate(canceled ? Math.round(drag.startIdx) : Math.round(finalFloat), { animate: false });
      };
      toggle.addEventListener("pointerup", finishDrag);
      toggle.addEventListener("pointercancel", (e) => finishDrag(e, { canceled: true }));

      options.forEach((opt, i) => {
        opt.setAttribute("role", "tab");
        opt.addEventListener("click", (e) => {
          if (drag.moved) {
            e.preventDefault();
            e.stopPropagation();
            return;
          }
          activate(i);
        });
        opt.addEventListener("keydown", (e) => {
          if (e.key === "ArrowRight" || e.key === "ArrowDown") {
            e.preventDefault();
            const next = (i + 1) % count;
            activate(next);
            options[next].focus();
          } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
            e.preventDefault();
            const prev = (i - 1 + count) % count;
            activate(prev);
            options[prev].focus();
          } else if (e.key === "Home") {
            e.preventDefault();
            activate(0);
            options[0].focus();
          } else if (e.key === "End") {
            e.preventDefault();
            activate(count - 1);
            options[count - 1].focus();
          } else if (e.key === " " || e.key === "Enter") {
            e.preventDefault();
            activate(i);
          }
        });
      });
      toggle.addEventListener("animationend", (e) => {
        if (e.animationName === "segment-stretch") toggle.classList.remove("is-stretching");
        if (e.animationName === "segment-fade") toggle.classList.remove("is-settling");
      });

      const startIdx = options.findIndex((o) => o.dataset.value === toggle.dataset.value);
      activate(startIdx >= 0 ? startIdx : 0, { animate: false });
    });
  }

  // ─── Small DOM builders shared by the views ───────────────────
  // ─── The chooser: one of N ────────────────────────────────────
  // WAI-ARIA's select-only combobox — a button plus a listbox popup, DOM focus
  // never leaving the button, the active row named by aria-activedescendant.
  //
  // This replaced the native <select>, which had one text slot per option. That
  // slot is why operators read `generic_soap` instead of "Generic SOAP": the
  // controller already sends a description alongside every name and the app had
  // nowhere to put it. It is also the one control the browser tests could not
  // see, because the popup an OS draws is not in the page.
  //
  // The trigger keeps a `value` property and fires a bubbling `change`, so every
  // caller that read `pick("pack").value` off a <select> still works.

  const TYPEAHEAD_MS = 800;

  function makeChooser(id) {
    const wrap = document.createElement("div");
    wrap.className = "chooser";

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.id = id;
    trigger.className = "chooser-trigger";
    trigger.setAttribute("role", "combobox");
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-controls", `${id}-list`);
    trigger.setAttribute("aria-labelledby", `${id}-label`);

    const value = document.createElement("span");
    value.className = "chooser-value";
    const chevron = document.createElement("span");
    chevron.className = "chooser-chevron";
    chevron.setAttribute("aria-hidden", "true");
    trigger.appendChild(value);
    trigger.appendChild(chevron);

    const list = document.createElement("ul");
    list.id = `${id}-list`;
    list.className = "chooser-list";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-labelledby", `${id}-label`);
    list.hidden = true;

    wrap.appendChild(trigger);
    wrap.appendChild(list);
    wireChooser(trigger, list, value);
    return wrap;
  }

  function wireChooser(trigger, list, valueSlot) {
    let entries = [];
    let selected = -1;
    let active = -1;
    let typed = "";
    let typedAt = 0;

    const rows = () => $$(".chooser-row", list);
    const isOpen = () => !list.hidden;

    const paint = () => {
      rows().forEach((row, i) => {
        row.setAttribute("aria-selected", i === selected ? "true" : "false");
        row.classList.toggle("is-active", isOpen() && i === active);
      });
      valueSlot.textContent = selected >= 0 ? entries[selected].label : "";
      // Removed rather than emptied. `aria-activedescendant=""` is an IDREF
      // naming no element, which is invalid — Chromium shrugs, and a shrug is
      // not a contract.
      if (isOpen() && active >= 0) {
        trigger.setAttribute("aria-activedescendant", rows()[active].id);
      } else {
        trigger.removeAttribute("aria-activedescendant");
      }
    };

    const moveTo = (index) => {
      if (!entries.length) return;
      active = Math.max(0, Math.min(entries.length - 1, index));
      paint();
      const row = rows()[active];
      if (row) row.scrollIntoView({ block: "nearest" });
    };

    const open = () => {
      if (isOpen() || !entries.length) return;
      list.hidden = false;
      // Flip above when the popup would run past the window bottom. Measured
      // after it is laid out, because until then it has no height.
      const box = trigger.getBoundingClientRect();
      const overruns = box.bottom + list.offsetHeight + 8 > window.innerHeight;
      list.classList.toggle("flips-up", overruns && box.top > list.offsetHeight);
      trigger.setAttribute("aria-expanded", "true");
      moveTo(selected >= 0 ? selected : 0);
    };

    const close = () => {
      if (!isOpen()) return;
      list.hidden = true;
      list.classList.remove("flips-up");
      trigger.setAttribute("aria-expanded", "false");
      paint();
    };

    const select = (index, opts) => {
      if (index < 0 || index >= entries.length) return;
      const changed = index !== selected;
      selected = index;
      paint();
      if (changed && (!opts || opts.notify !== false)) {
        trigger.dispatchEvent(new Event("change", { bubbles: true }));
      }
    };

    const commit = () => {
      select(active);
      close();
      trigger.focus();
    };

    // Typeahead matches on what a person reads — the label, not the id.
    const typeahead = (char) => {
      const now = Date.now();
      typed = now - typedAt > TYPEAHEAD_MS ? char : typed + char;
      typedAt = now;
      const from = (isOpen() ? active : selected) + (typed.length === 1 ? 1 : 0);
      for (let step = 0; step < entries.length; step += 1) {
        const i = (Math.max(0, from) + step) % entries.length;
        if (entries[i].label.toLowerCase().startsWith(typed.toLowerCase())) {
          if (isOpen()) moveTo(i);
          else select(i);
          return;
        }
      }
    };

    trigger.addEventListener("click", () => (isOpen() ? close() : open()));

    trigger.addEventListener("keydown", (e) => {
      const key = e.key;
      if (!isOpen()) {
        if (key === "ArrowDown" || key === "ArrowUp" || key === "Enter" || key === " ") {
          e.preventDefault();
          open();
        } else if (key === "Home") {
          e.preventDefault();
          select(0);
        } else if (key === "End") {
          e.preventDefault();
          select(entries.length - 1);
        } else if (key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) {
          e.preventDefault();
          typeahead(key);
        }
        return;
      }
      if (key === "ArrowDown") {
        e.preventDefault();
        moveTo(active + 1);
      } else if (key === "ArrowUp") {
        e.preventDefault();
        moveTo(active - 1);
      } else if (key === "Home") {
        e.preventDefault();
        moveTo(0);
      } else if (key === "End") {
        e.preventDefault();
        moveTo(entries.length - 1);
      } else if (key === "PageDown") {
        e.preventDefault();
        moveTo(active + 10);
      } else if (key === "PageUp") {
        e.preventDefault();
        moveTo(active - 10);
      } else if (key === "Enter" || key === " ") {
        e.preventDefault();
        commit();
      } else if (key === "Escape") {
        e.preventDefault();
        close();
      } else if (key === "Tab") {
        // APG: tabbing out of an open listbox commits what is focused.
        select(active);
        close();
      } else if (key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        typeahead(key);
      }
    });

    list.addEventListener("mousedown", (e) => {
      // Keep DOM focus on the trigger: the popup is not a focus destination.
      e.preventDefault();
      const row = e.target.closest(".chooser-row");
      if (!row) return;
      active = rows().indexOf(row);
      commit();
    });
    list.addEventListener("mousemove", (e) => {
      const row = e.target.closest(".chooser-row");
      if (row) moveTo(rows().indexOf(row));
    });

    document.addEventListener("mousedown", (e) => {
      if (!isOpen()) return;
      if (!trigger.parentNode.contains(e.target)) close();
    });

    Object.defineProperty(trigger, "value", {
      get: () => (selected >= 0 ? entries[selected].value : ""),
      set: (next) => {
        const i = entries.findIndex((entry) => entry.value === next);
        if (i >= 0) select(i, { notify: false });
      },
    });

    CHOOSERS.set(trigger, (next) => {
      entries = next;
      list.innerHTML = "";
      next.forEach((entry, i) => {
        const row = document.createElement("li");
        row.id = `${trigger.id}-opt-${i}`;
        row.className = "chooser-row";
        row.setAttribute("role", "option");
        row.dataset.value = entry.value;
        // The machine id stays reachable on the tooltip even when the caption
        // is a description — the same rule the stage rail and the upload states
        // follow: plain English on screen, the id for whoever has to ask.
        if (entry.value) row.title = entry.value;
        const name = document.createElement("span");
        name.className = "chooser-name";
        name.textContent = entry.label;
        row.appendChild(name);
        if (entry.note) {
          const note = document.createElement("span");
          note.className = "chooser-note";
          note.textContent = entry.note;
          row.appendChild(note);
        }
        list.appendChild(row);
      });
      selected = next.length ? 0 : -1;
      active = selected;
      close();
      paint();
    });
  }

  const CHOOSERS = new Map();

  //: Wire the choosers written directly into index.html. The ones the run form
  //: builds are wired as they are made.
  function initChoosers(root) {
    for (const wrap of $$(".chooser", root || document)) {
      const trigger = $(".chooser-trigger", wrap);
      const list = $(".chooser-list", wrap);
      const value = $(".chooser-value", wrap);
      if (trigger && list && value && !CHOOSERS.has(trigger)) {
        wireChooser(trigger, list, value);
      }
    }
  }

  // Acronyms a person would write in capitals. Anything else is title-cased,
  // which is right for the vendor names these ids are built from.
  const SHOUTED = new Set(["cda", "csv", "ehi", "fhir", "hl7", "pdf", "pf", "soap", "tsv", "xml"]);

  //: A readable name for a machine id: `generic_soap` -> "Generic SOAP",
  //: `pf-tebra` -> "PF Tebra". Every place a person reads one of these ids used
  //: to print it raw, because a <select> option had one text slot and the id
  //: had to go in it. The id survives as the row's caption.
  //:
  //: A guess, and only ever a guess — `ccda` will not become "C-CDA" here, and
  //: it used to be a hard-coded exception for exactly that reason. Sources and
  //: layouts now declare their own name, so this is the fallback for the ones
  //: that do not: destinations, and third-party packs written before the field
  //: existed. See `nameOf`.
  function displayName(id) {
    return String(id)
      .split(/[-_\s]+/)
      .filter(Boolean)
      .map((word) =>
        SHOUTED.has(word.toLowerCase())
          ? word.toUpperCase()
          : word.charAt(0).toUpperCase() + word.slice(1)
      )
      .join(" ");
  }

  //: What a registration says its name is, or a guess from its id. One rule,
  //: so a pack and a source and a destination are all read the same way and
  //: no caller has to remember which of them declares one.
  function nameOf(entry) {
    return (entry && entry.display) || displayName(entry && entry.name);
  }

  //: Entries are {value, label, note}. `note` is the caption under the name —
  //: the raw id, or the source's own description — which the <select> had no
  //: room for and therefore discarded.
  function fillChooser(trigger, entries) {
    const fill = trigger && CHOOSERS.get(trigger);
    if (fill) fill(entries);
  }

  function renderSectionMatrix(matrix, sections, emptyText, remembered) {
    if (!matrix) return;
    matrix.innerHTML = "";
    const keys = Object.keys(sections || {});
    if (keys.length === 0) {
      matrix.textContent = emptyText;
      matrix.classList.add("empty");
      return;
    }
    matrix.classList.remove("empty");
    for (const key of keys) {
      const flag = sections[key];
      const label = document.createElement("label");
      label.className = "toggle";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.dataset.section = key;
      // The layout's default, unless this person already said otherwise for
      // this layout. Reinstating the default over a deliberate choice would
      // put a section back into the chart that they had turned off.
      input.checked =
        remembered && key in remembered ? !!remembered[key] : flag.default !== false;
      const track = document.createElement("span");
      track.className = "track";
      const text = document.createElement("span");
      text.textContent = flag.label || key;
      label.appendChild(input);
      label.appendChild(track);
      label.appendChild(text);
      matrix.appendChild(label);
    }
  }

  // Scoped to inputs carrying data-section so a future non-section checkbox in
  // the same container can never pollute the map.
  function gatherSections(matrix) {
    const sections = {};
    if (!matrix) return sections;
    for (const box of $$("input[data-section]", matrix)) sections[box.dataset.section] = box.checked;
    return sections;
  }

  function renderPatients(panel, body, patients) {
    if (!panel || !body) return;
    body.innerHTML = "";
    if (!patients.length) {
      // Distinct from the panel's own "No run yet": a run HAS happened and it
      // found nobody. `clearPatients` still resets to the default copy, so the
      // three states — not run yet, found nobody, could not be read — no longer
      // wear the same words.
      setEmpty(panel.id, true, {
        title: "No patients in this run.",
        detail: "The run finished and found no patient records to rebuild.",
      });
      return;
    }
    const table = document.createElement("table");
    table.className = "patients-table";
    // A <thead> and `scope="col"` — without them the four headings were a
    // first row of bold text, and the cells below carried no column with them:
    // "Ada Lovelace 1815-12-10 3 12" instead of "Patient: …, Visits: 3".
    const thead = document.createElement("thead");
    const head = document.createElement("tr");
    for (const heading of ["Patient", "Date of birth", "Visits", "Notes"]) {
      const th = document.createElement("th");
      th.scope = "col";
      th.textContent = heading;
      head.appendChild(th);
    }
    thead.appendChild(head);
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    for (const p of patients) {
      const tr = document.createElement("tr");
      for (const value of [p.display_name || "—", p.birth_date || "—", String(p.encounters), String(p.documents)]) {
        const td = document.createElement("td");
        td.textContent = value; // textContent: patient data as text, never markup
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    body.appendChild(table);
    setEmpty(panel.id, false);
  }

  //: One result row. `cells` is [{text, className}] in column order; `bucket`
  //: picks the tint, and the row's own plain-English text is what actually
  //: carries the state — the tint only reinforces it, because at equal weight
  //: amber and red are the same colour to a deuteranope.
  function resultRow(bucket, cells) {
    const row = document.createElement("div");
    row.className = "result";
    if (bucket) row.dataset.bucket = bucket;
    for (const cell of cells) {
      if (cell === null || cell === undefined) continue;
      const span = document.createElement("span");
      if (cell.className) span.className = cell.className;
      span.textContent = cell.text;
      row.appendChild(span);
    }
    return row;
  }

  //: A region keeps its heading whether or not it has rows; what swaps is the
  //: list and the one sentence saying what would put rows in it. Every empty
  //: state is `<listId>-empty` in the markup, so there is nothing to wire.
  // `message` (optional) is {title, detail} — a state the markup has no words
  // for, such as "the detail could not be read". Omit it and the panel's OWN
  // copy comes back, so the wording still lives in exactly one place.
  function setEmpty(listId, isEmpty, message) {
    const list = el(listId);
    const empty = el(`${listId}-empty`);
    if (list) list.hidden = isEmpty;
    // A column header over a "nothing here yet" message is a table promising
    // rows that are not coming. Named `<listId>-head` in the markup, so this
    // needs no wiring either — the same convention as `-empty`.
    const head = el(`${listId}-head`);
    if (head) head.hidden = isEmpty;
    if (!empty) return;
    empty.hidden = !isEmpty;
    const title = empty.querySelector("b");
    const detail = empty.querySelector("span");
    if (!title || !detail) return;
    if (empty.dataset.defaultTitle === undefined) {
      empty.dataset.defaultTitle = title.textContent || "";
      empty.dataset.defaultDetail = detail.textContent || "";
    }
    title.textContent = message ? message.title : empty.dataset.defaultTitle;
    detail.textContent = message ? message.detail : empty.dataset.defaultDetail;
  }

  function clearPatients(panel, body) {
    if (body) body.innerHTML = "";
    if (panel) setEmpty(panel.id, true);
  }

  // `summaryId` is the run's OWN id (from its `done` event), so a rapid second
  // run cannot replace the detail this run is about to show. Advisory: a
  // failure never blocks the run roll-up.
  async function loadPatients(panel, body, summaryId) {
    if (!hasApi()) return;
    // A failure here used to be swallowed whole, leaving the panel showing the
    // copy it had before the run: "No run yet. Choose Rebuild charts above…"
    // — under a status line reading "Finished." and a strip line counting the
    // patients. It read identically to a run that genuinely found nobody, and
    // those want different things from the operator: one is a reason to go and
    // look at the output folder, the other is not.
    let reason = "";
    try {
      const res = await window.pywebview.api.last_run_summary(summaryId);
      if (res && res.ok) {
        renderPatients(panel, body, res.patients || []);
        return;
      }
      reason = (res && res.error) || "no answer from the app";
    } catch (err) {
      reason = String(err);
    }
    if (!panel || !body) return;
    body.innerHTML = "";
    setEmpty(panel.id, true, {
      title: "The per-patient list could not be read.",
      detail:
        `The run itself finished — this is the roll-up beside it (${reason}). ` +
        "The charts are in the output folder.",
    });
  }

  // ─── The shared run form ──────────────────────────────────────
  // Charts owns the plain rebuild; Migrate composes the same component beside a
  // destination picker. There is exactly one implementation of these fields —
  // the two views differ only in `mode`.
  //: `for` only reaches a control the HTML spec calls labelable. "Chart
  //: sections" pointed at a <div> holding four checkboxes, so the association
  //: was dropped and the group had no name at all — the checkboxes were just a
  //: run of switches. A <div> gets `role="group"` named by the same label
  //: instead, which is the association `for` could not make.
  const LABELABLE = new Set(["BUTTON", "INPUT", "METER", "OUTPUT", "PROGRESS", "SELECT", "TEXTAREA"]);

  function makeField(id, labelText, helpText, control) {
    const field = document.createElement("div");
    field.className = "field";
    const label = document.createElement("label");
    label.id = `${id}-label`;
    label.textContent = labelText;
    if (LABELABLE.has(control.tagName)) {
      label.setAttribute("for", id);
    } else {
      control.setAttribute("role", "group");
      control.setAttribute("aria-labelledby", label.id);
    }
    field.appendChild(label);
    field.appendChild(control);
    if (helpText) {
      const help = document.createElement("div");
      help.className = "field-help";
      help.textContent = helpText;
      field.appendChild(help);
    }
    return field;
  }

  // `face` is the §4.4 opt-in for machine-shaped content ("is-path", "is-id"):
  // the caller knows what kind of string the field holds, the builder does not.
  function makeInput(id, placeholder, face) {
    const input = document.createElement("input");
    input.type = "text";
    input.id = id;
    if (face) input.className = face;
    if (placeholder) input.placeholder = placeholder;
    return input;
  }

  function makeToggle(id, labelText, checked) {
    const label = document.createElement("label");
    label.className = "toggle";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.id = id;
    input.checked = !!checked;
    const track = document.createElement("span");
    track.className = "track";
    const text = document.createElement("span");
    text.textContent = labelText;
    label.appendChild(input);
    label.appendChild(track);
    label.appendChild(text);
    return label;
  }

  // A switch plus the sentence explaining what it turns on. The label names the
  // thing being enabled, never "on/off" — that is what a switch already says.
  function makeSwitchField(id, labelText, helpText, checked) {
    const wrap = document.createElement("div");
    wrap.className = "stack";
    wrap.appendChild(makeToggle(id, labelText, checked));
    if (helpText) {
      const help = document.createElement("div");
      help.className = "field-help";
      help.textContent = helpText;
      wrap.appendChild(help);
    }
    return wrap;
  }

  // opts: {prefix, mode: "charts" | "migrate", runLabel, onRun}
  function buildRunForm(host, opts) {
    if (!host) return null;
    const prefix = opts.prefix;
    const migrate = opts.mode === "migrate";
    const id = (name) => `${prefix}-${name}`;
    host.innerHTML = "";
    host.className = "stack";

    host.appendChild(
      makeField(
        id("export-dir"),
        "Export folder",
        "The folder your EHR gave you when you exported your records.",
        makeInput(id("export-dir"), "C:\\Users\\you\\Downloads\\ehr-export", "is-path")
      )
    );
    if (migrate) {
      // Migrate detects the format from the export before anything else runs.
      const actions = document.createElement("div");
      actions.className = "actions";
      const detect = document.createElement("button");
      detect.type = "button";
      detect.className = "btn btn-secondary";
      detect.id = id("detect");
      detect.textContent = "Detect the format";
      actions.appendChild(detect);
      host.appendChild(actions);
    }
    host.appendChild(
      makeField(
        id("source"),
        "Export format",
        // Migrate's button fills this in; Charts' chooser says "Detect" itself
        // and each row carries the format's own description.
        migrate ? "Detect the format fills this in for you." : "",
        makeChooser(id("source"))
      )
    );
    host.appendChild(
      makeField(
        id("out-dir"),
        "Where results go",
        "",
        makeInput(id("out-dir"), "C:\\Users\\you\\Documents\\anastomosis-charts", "is-path")
      )
    );
    host.appendChild(
      migrate
        ? makeField(
            id("render"),
            "Chart pages",
            "",
            makeChooser(id("render"))
          )
        : makeField(
            id("pack"),
            "Chart layout",
            "",
            makeChooser(id("pack"))
          )
    );

    const matrix = document.createElement("div");
    matrix.className = "section-matrix";
    matrix.id = id("sections");
    host.appendChild(makeField(id("sections"), "Chart sections", "", matrix));

    host.appendChild(
      makeSwitchField(
        id("qa"),
        "Double-check results",
        "Re-reads every finished chart and confirms names, dates, and values landed on the right patient.",
        true
      )
    );

    const toggles = document.createElement("div");
    toggles.className = "toggles";
    if (!migrate) {
      toggles.appendChild(makeToggle(id("archive"), "Save an archive copy"));
      toggles.appendChild(makeToggle(id("bundle"), "Save the data files"));
      toggles.appendChild(makeToggle(id("ccda"), "Save a C-CDA transfer document"));
    }
    toggles.appendChild(makeToggle(id("force"), "Rebuild pages even if unchanged"));
    host.appendChild(toggles);

    if (!migrate) {
      const upload = document.createElement("div");
      upload.className = "toggles";
      upload.appendChild(makeToggle(id("write-manifest"), "Prepare for upload"));
      host.appendChild(upload);
      const uploadHelp = document.createElement("div");
      uploadHelp.className = "field-help";
      uploadHelp.textContent =
        "Also writes the files the Uploads screen needs to file these charts into another system.";
      host.appendChild(uploadHelp);
    }

    // One Advanced disclosure per form, closed by default (§10.8).
    const advanced = document.createElement("details");
    advanced.className = "advanced";
    const summary = document.createElement("summary");
    summary.textContent = "Advanced";
    advanced.appendChild(summary);
    const body = document.createElement("div");
    body.className = "advanced-body";
    body.appendChild(
      makeField(
        id("pack-dir"),
        "Additional layout folder",
        "Only needed for a chart layout that did not ship with Anastomosis.",
        makeInput(id("pack-dir"), "C:\\Users\\you\\Documents\\layouts", "is-path")
      )
    );
    const trustWrap = document.createElement("div");
    trustWrap.className = "stack";
    const trustToggles = document.createElement("div");
    trustToggles.className = "toggles";
    trustToggles.appendChild(makeToggle(id("trust-pack"), "Allow this new layout to run"));
    trustWrap.appendChild(trustToggles);
    const trustHelp = document.createElement("div");
    trustHelp.className = "field-help";
    trustHelp.textContent =
      "Layouts contain code. Anastomosis refuses layouts it has not seen before unless you allow them once here.";
    trustWrap.appendChild(trustHelp);
    body.appendChild(trustWrap);
    advanced.appendChild(body);
    host.appendChild(advanced);

    const actions = document.createElement("div");
    actions.className = "actions";
    const run = document.createElement("button");
    run.type = "button";
    run.className = "btn btn-primary";
    run.id = id("run");
    run.textContent = opts.runLabel || "Rebuild charts";
    actions.appendChild(run);
    host.appendChild(actions);

    initSegmentToggles(host);
    const pick = (name) => el(id(name));
    // What this person actually chose, per layout. Without it, switching layout
    // and back silently reinstated the new layout's defaults — and those
    // defaults were what the run was built from.
    const chosenByLayout = Object.create(null);
    let sectionsKey = null;
    if (typeof opts.onRun === "function") run.addEventListener("click", opts.onRun);

    return {
      root: host,
      id,
      el: pick,
      setSections(sections, emptyText, key) {
        // Remember what was chosen for the layout being left, so coming back to
        // it restores those choices rather than the layout's defaults.
        if (sectionsKey !== null) chosenByLayout[sectionsKey] = gatherSections(matrix);
        sectionsKey = key === undefined || key === null ? null : String(key);
        renderSectionMatrix(
          matrix,
          sections,
          emptyText,
          sectionsKey === null ? null : chosenByLayout[sectionsKey]
        );
      },
      //: The DOM id this form gave a named field, so a caller can point at it
      //: (to open the disclosure hiding it) without re-deriving the prefix.
      idFor: (name) => id(name),
      setBusy(busy) {
        run.disabled = busy;
        run.textContent = busy ? "Rebuilding…" : opts.runLabel || "Rebuild charts";
      },
      setOffline() {
        run.disabled = true;
        run.textContent = opts.runLabel || "Rebuild charts";
      },
      values() {
        const packDir = pick("pack-dir") ? pick("pack-dir").value.trim() : "";
        const checked = (name) => !!(pick(name) && pick(name).checked);
        const value = (name) => (pick(name) ? pick(name).value : "");
        return {
          exportDir: value("export-dir"),
          outDir: value("out-dir"),
          // An empty pick means "detect" — the controller reads null as "sniff".
          source: value("source") || null,
          pack: migrate ? null : value("pack"),
          render: migrate ? value("render") || "neutral" : null,
          sections: gatherSections(matrix),
          qa: checked("qa"),
          archive: checked("archive"),
          bundle: checked("bundle"),
          ccda: checked("ccda"),
          force: checked("force"),
          packDirs: packDir ? [packDir] : [],
          trustNew: checked("trust-pack"),
          writeManifest: checked("write-manifest"),
        };
      },
    };
  }

  // ─── Calendar (halo cells + count badges) ─────────────────────
  const MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
  ];
  const isoDate = (y, m, d) => `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
  const daysInMonth = (year, monthIdx) => new Date(year, monthIdx + 1, 0).getDate();

  // Mon-first month grid. `histogram` maps ISO date → {pending, done, errors}
  // (counts only). Halo priority: errors > pending > done; a total above 1
  // paints the count badge.
  function renderCalendar(opts) {
    const grid = opts.gridEl;
    if (!grid) return;
    if (opts.titleEl) opts.titleEl.textContent = `${MONTH_NAMES[opts.month]} ${opts.year}`;
    grid.innerHTML = "";
    const year = opts.year;
    const month = opts.month;
    const histogram = opts.histogram || {};
    const leading = (new Date(year, month, 1).getDay() + 6) % 7; // Sun-first → Mon-first
    const total = daysInMonth(year, month);
    const now = new Date();
    const todayIso = isoDate(now.getFullYear(), now.getMonth(), now.getDate());
    const prevMonth = month === 0 ? 11 : month - 1;
    const prevYear = month === 0 ? year - 1 : year;
    const prevTotal = daysInMonth(prevYear, prevMonth);
    const nextYear = month === 11 ? year + 1 : year;
    const nextMonth = month === 11 ? 0 : month + 1;

    const cells = [];
    for (let i = 0; i < leading; i++) {
      cells.push({ outside: true, y: prevYear, m: prevMonth, d: prevTotal - leading + 1 + i });
    }
    for (let d = 1; d <= total; d++) cells.push({ outside: false, y: year, m: month, d });
    let trailing = 1;
    while (cells.length < 42) cells.push({ outside: true, y: nextYear, m: nextMonth, d: trailing++ });

    // Six rows of seven. The row wrappers exist for the accessibility tree
    // only (`.cal-week { display: contents }` keeps the cells where the CSS
    // grid expects them); without them the table had no rows and reported
    // nothing to count.
    let row = null;
    cells.forEach((cell, index) => {
      if (index % 7 === 0) {
        row = document.createElement("div");
        row.className = "cal-week";
        row.setAttribute("role", "row");
        grid.appendChild(row);
      }
      const day = document.createElement("div");
      day.className = "calendar-cell";
      day.setAttribute("role", "cell");
      const iso = isoDate(cell.y, cell.m, cell.d);
      day.dataset.iso = iso;
      const label = document.createElement("span");
      label.textContent = String(cell.d);
      day.appendChild(label);
      if (cell.outside) day.classList.add("calendar-cell--outside");
      if (iso === todayIso) day.classList.add("calendar-cell--today");
      if (!cell.outside) {
        const hit = histogram[iso];
        if (hit) {
          const pending = hit.pending || 0;
          const done = hit.done || 0;
          const errors = hit.errors || 0;
          day.classList.add("calendar-cell--has-data");
          if (errors > 0) day.classList.add("calendar-cell--halo-errors");
          else if (pending > 0) day.classList.add("calendar-cell--halo-pending");
          else if (done > 0) day.classList.add("calendar-cell--halo-done");
          const sum = pending + done + errors;
          if (sum > 1) {
            const badge = document.createElement("span");
            badge.className = "calendar-count-badge";
            badge.textContent = sum > 99 ? "99+" : String(sum);
            day.appendChild(badge);
          }
          // The badge counts RUNS, and nothing on screen said so — a cell for
          // day 3 carrying a badge of 2 read out as the number "32". The state
          // is in here too, in the legend's own words, because the halo that
          // carries it is a colour and the legend is a colour key.
          const states = [
            [errors, "needs attention"],
            [pending, "in progress"],
            [done, "filed"],
          ].filter((pair) => pair[0] > 0);
          const detail =
            states.length === 1
              ? states[0][1]
              : states.map((pair) => `${pair[0]} ${pair[1]}`).join(" and ");
          day.setAttribute(
            "aria-label",
            `${cell.d} ${MONTH_NAMES[month]} — ` +
              `${sum} filing ${sum === 1 ? "run" : "runs"}, ${detail}`
          );
        }
      }
      row.appendChild(day);
    });
  }

  // ─── Tabs (one workspace, several modes) ─────────────────────
  // Generic chrome: every [role="tab"] in a .mode-tabs group shows the panel
  // named by its aria-controls and hides its siblings'.
  //
  // The keyboard half was missing, so a screen reader announced "tab, 1 of 2"
  // and then the arrow keys did nothing — and both tabs were separate stops on
  // the way through the page. A tablist is ONE stop with the arrows moving
  // inside it: that is the roving tabindex below, and it is the same contract
  // the nav pill already keeps.
  function initTabs(root) {
    for (const group of $$(".mode-tabs", root || document)) {
      const tabs = $$('[role="tab"]', group);
      if (!tabs.length) continue;
      const show = (chosen, moveFocus) => {
        for (const tab of tabs) {
          const selected = tab === chosen;
          tab.setAttribute("aria-selected", selected ? "true" : "false");
          tab.tabIndex = selected ? 0 : -1;
          const panel = el(tab.getAttribute("aria-controls"));
          if (panel) panel.hidden = !selected;
        }
        if (moveFocus) chosen.focus();
      };
      for (const tab of tabs) {
        tab.addEventListener("click", () => show(tab, false));
        tab.addEventListener("keydown", (e) => {
          const at = tabs.indexOf(tab);
          let next = null;
          if (e.key === "ArrowRight" || e.key === "ArrowDown") {
            next = tabs[(at + 1) % tabs.length];
          } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
            next = tabs[(at - 1 + tabs.length) % tabs.length];
          } else if (e.key === "Home") {
            next = tabs[0];
          } else if (e.key === "End") {
            next = tabs[tabs.length - 1];
          }
          if (!next) return;
          e.preventDefault();
          show(next, true);
        });
      }
      // Whatever the markup says is selected keeps the only tab stop.
      show(tabs.find((tab) => tab.getAttribute("aria-selected") === "true") || tabs[0], false);
    }
  }

  // ─── About popover ────────────────────────────────────────────
  // WebView2 does not report `prefers-reduced-transparency` on every Windows
  // build, and an accessibility setting that silently does nothing on the
  // platform the app ships on is worse than not having one. So the same tokens
  // are also reachable by hand, and the choice survives a restart — this is
  // the one preference the app stores, and it holds no information about
  // anyone, so localStorage is the right size of thing for it.
  const REDUCE_EFFECTS_KEY = "anast.reduce-effects";

  function initReduceEffects() {
    const box = el("reduce-effects");
    if (!box) return;
    let saved = null;
    try {
      saved = window.localStorage.getItem(REDUCE_EFFECTS_KEY);
    } catch (_) {
      /* a browser with storage disabled simply gets the system preference */
    }
    const apply = (on) => {
      document.documentElement.dataset.reduceEffects = on ? "true" : "false";
    };
    box.checked = saved === "true";
    apply(box.checked);
    box.addEventListener("change", () => {
      apply(box.checked);
      try {
        window.localStorage.setItem(REDUCE_EFFECTS_KEY, String(box.checked));
      } catch (_) {
        /* the setting still applies for this session */
      }
    });
  }

  function initAbout() {
    const popover = el("about-popover");
    if (!popover) return;
    initDisclosure({
      trigger: el("about-btn"),
      panel: popover,
      isOpen: () => !popover.hidden,
      show: () => {
        popover.hidden = false;
      },
      hide: () => {
        popover.hidden = true;
      },
    });
  }

  // ─── Boot ─────────────────────────────────────────────────────
  // Runs immediately (painting the offline notice when there is genuinely no
  // bridge — the plain-browser preview) and again when the api lands, because
  // pywebview attaches it asynchronously after DOM ready. The `pywebviewready`
  // event alone is NOT a safe wake-up: in the frozen app the bridge can attach
  // and fire it before this script's listener exists, and a missed one-shot
  // event would strand a healthy app on the offline notice — so a poll backs
  // the event, and whichever sees the api first wins. This is the app's ONLY
  // bridge bootstrap; no view re-races it.
  let bootedLive = false;
  let bootPoll = null;
  function boot() {
    const live = hasApi();
    if (live) {
      if (bootedLive) return; // event + poll may both land; boot live once
      bootedLive = true;
      if (bootPoll) {
        clearInterval(bootPoll);
        bootPoll = null;
      }
    }
    document.documentElement.dataset.bridge = live ? "live" : "offline";
    const notice = el("no-api");
    if (notice) notice.classList.toggle("show", !live);
    if (live) loadInfo();
    for (const cb of READY) cb(live);
    if (!live) {
      window.addEventListener("pywebviewready", boot, { once: true });
      if (!bootPoll) {
        bootPoll = setInterval(() => {
          if (hasApi()) boot();
        }, 150);
      }
    }
  }

  function init() {
    paintIcons(document);
    initAbout();
    initReduceEffects();
    initTabs(document);
    initChoosers(document);
    // The nav is the sliding pill, wired like any other: the only thing that
    // makes it the nav is what its change does.
    initSegmentToggles($(".navbar"), (name, value) => {
      if (name === "view") showView(value);
    });
    // Buttons OUTSIDE the pill that jump to a view (Migrate's handoff).
    for (const btn of $$("[data-view-target]")) {
      if (btn.closest(".navpill")) continue;
      btn.addEventListener("click", () => showView(btn.dataset.viewTarget));
    }
    const dismiss = el("banner-dismiss");
    if (dismiss) dismiss.addEventListener("click", hideBanner);
    initLogDrawer();
    const closeBtn = el("log-drawer-close");
    if (closeBtn) closeBtn.addEventListener("click", () => logDrawer && logDrawer.close(true));
    // The strip advertises "L", so a bare letter it stays — but the guard was
    // two tag names, and everything else on the page is neither. Pressing `l`
    // on a chooser trigger, a checkbox, a button or a nav tab opened the
    // activity drawer.
    document.addEventListener("keydown", (e) => {
      if (e.key !== "l" && e.key !== "L") return;
      // Ctrl+L, Cmd+L and Alt+L belong to the browser and the window manager.
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      // Something nearer the key already claimed it. A chooser's type-ahead
      // calls preventDefault() for every printable character — open or closed
      // — and does not stop propagation, so both handlers used to run.
      if (e.defaultPrevented) return;
      if (takesTyping(document.activeElement)) return;
      e.preventDefault();
      if (logDrawer) logDrawer.toggle();
    });
    boot();
  }

  // ─── Required fields ──────────────────────────────────────────
  //
  // Uploads checked its inputs; Charts, Migrate and both Teach modes did not.
  // Clicking "Rebuild charts" on a fresh page sent `run_pipeline_async("", "",
  // …)`, locked the button to "Rebuilding…", and said nothing about what was
  // missing — while also taking the process-wide busy guard, so the real run
  // the operator started next was refused.
  //
  // `fields` is [[value, "what it is", elementId], …]. Returns true when
  // everything is filled; otherwise banners the blank ones BY NAME, opens any
  // disclosure hiding one, and returns false.
  function requireFields(fields) {
    const missing = fields.filter(([value]) => !String(value || "").trim());
    if (!missing.length) return true;
    missing.forEach(([, , id]) => {
      const field = id && document.getElementById(id);
      const details = field && field.closest("details");
      if (details) details.open = true;
    });
    const names = missing.map(([, name]) => name);
    const phrase =
      names.length === 1
        ? names[0]
        : names.length === 2
          ? `${names[0]} and ${names[1]}`
          : `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
    showBanner(`Fill in ${phrase}.`);
    return false;
  }

  // ─── One button, one job at a time ────────────────────────────
  //
  // `buildRunForm.setBusy` gave Charts and Migrate this, and nothing else had
  // it: three rapid clicks on "Start filing" sent three `upload_start` calls.
  // The Python side refuses the second and third, so the operator saw a red
  // error banner for a run that was proceeding normally — and after the click
  // that WORKED, the button was unchanged and the counters did not move for a
  // full poll interval, so the screen's plainest reading was "nothing
  // happened".
  //
  // `finally`, not the success path: a button that stays disabled after a
  // failure is a dead screen, which is the worse of the two bugs.
  async function guardButton(button, busyLabel, work) {
    if (!button || button.disabled) return undefined;
    const label = button.textContent;
    button.disabled = true;
    if (busyLabel) button.textContent = busyLabel;
    try {
      return await work();
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }

  // ─── What the source offered, and what arrived ────────────────
  //
  // The source ledger's reading of a load: sentences composed on the Python
  // side out of counts and its own fixed words (ledger.physician_reading), so
  // no patient value can be in them, and this only lays them out. An empty or
  // missing reading hides the box — every source that keeps no ledger leaves
  // the screen exactly as it was.
  function renderReading(box, lines) {
    if (!box) return;
    box.textContent = "";
    if (!Array.isArray(lines) || !lines.length) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    const head = document.createElement("h3");
    head.textContent = "What the source offered, and what arrived";
    box.appendChild(head);
    for (const line of lines) {
      const p = document.createElement("p");
      p.textContent = String(line);
      box.appendChild(p);
    }
  }

  window.AnastShell = {
    hasApi,
    guardButton,
    requireFields,
    onReady,
    onInfo,
    icon,
    paintIcons,
    registerView,
    registerFlow,
    showView,
    currentView: () => CURRENT,
    logEvent,
    initDisclosure,
    resultRow,
    setEmpty,
    showBanner,
    hideBanner,
    announce,
    setStatus,
    stageLabel,
    initSegmentToggles,
    displayName,
    nameOf,
    // The chooser and the labelled field around it, so a view can build one of
    // N where its OPTIONS come from the file in front of the operator (Teach's
    // per-column corrections) rather than from markup written in advance.
    makeChooser,
    makeField,
    fillChooser,
    renderSectionMatrix,
    gatherSections,
    buildRunForm,
    renderPatients,
    clearPatients,
    renderReading,
    refusalText,
    loadPatients,
    renderCalendar,
    MONTH_NAMES,
  };

  // The view scripts parse after this file and register synchronously, so they
  // are all present by the time DOMContentLoaded fires and boot() runs.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    window.setTimeout(init, 0);
  }
})();
