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

    for (const btn of $$("[data-view-target]")) {
      btn.setAttribute("aria-current", btn.dataset.viewTarget === name ? "true" : "false");
    }
    const band = el("view-band");
    if (band) band.textContent = (spec && spec.title) || name;

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
  const NON_COUNT_KEYS = ["type", "stage", "state", "flow", "summary_id", "notice", "outcome"];
  function countsText(event) {
    return Object.keys(event)
      .filter((k) => !NON_COUNT_KEYS.includes(k))
      .map((k) => `${k.replace(/_/g, " ")} ${event[k]}`)
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

  function openLogDrawer() {
    const drawer = el("log-drawer");
    if (!drawer) return;
    drawer.hidden = false;
    const strip = el("log-strip");
    if (strip) strip.setAttribute("aria-expanded", "true");
    const rows = el("log-rows");
    if (rows) rows.scrollTop = rows.scrollHeight;
  }
  function closeLogDrawer() {
    const drawer = el("log-drawer");
    if (!drawer) return;
    drawer.hidden = true;
    const strip = el("log-strip");
    if (strip) strip.setAttribute("aria-expanded", "false");
  }
  function toggleLogDrawer() {
    const drawer = el("log-drawer");
    if (!drawer) return;
    if (drawer.hidden) openLogDrawer();
    else closeLogDrawer();
  }

  // ─── Banner ───────────────────────────────────────────────────
  // The controller answers a submit it cannot take with a short machine-readable
  // sentinel, not a message — `Busy` is pinned by name across the controller
  // tests and has to stay a sentinel. This is the one place it becomes something
  // a physician can act on. Anything unrecognised is passed through as-is; a
  // `prefix` is only used on those, because "the samples could not be read:
  // Busy" describes a failure that never happened.
  const REFUSALS = {
    Busy: "Anastomosis is already working on something else. Wait for that to finish, then try again.",
  };
  function refusalText(error, prefix) {
    const sentinel = String(error == null ? "" : error);
    if (sentinel in REFUSALS) return REFUSALS[sentinel];
    return prefix ? `${prefix}: ${sentinel}` : sentinel;
  }

  function showBanner(message) {
    const banner = el("banner");
    if (!banner) return;
    banner.textContent = String(message);
    banner.classList.add("show");
  }
  function hideBanner() {
    const banner = el("banner");
    if (banner) banner.classList.remove("show");
  }

  // ─── Segment toggles (the gooey pill) ─────────────────────────
  // Carried from the Tebra reference: click/arrow-keys snap with a scaleX(1.45)
  // stretch; pointer-down + drag follows the cursor and snaps to the nearest
  // slot on release. --segment-count/--segment-index are written through the
  // CSSOM because the strict `style-src 'self'` CSP refuses a markup style="".
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
        const nextIdx = Math.max(0, Math.min(count - 1, Math.round(nextIdxRaw)));
        const fromFloat = parseFloat(toggle.style.getPropertyValue("--segment-index") || "0");
        const fromIdx = Math.round(fromFloat);
        const opt = options[nextIdx];
        if (!opt) return;
        const sameSlot = nextIdx === fromIdx && Math.abs(fromFloat - fromIdx) < 0.001;
        if (animate && sameSlot) return;

        const changed = opt.dataset.value !== toggle.dataset.value;
        toggle.dataset.value = opt.dataset.value;
        if (animate && nextIdx !== fromIdx && !prefersReduced()) {
          toggle.style.setProperty("--segment-from", String(fromIdx));
          toggle.classList.remove("is-stretching");
          void toggle.offsetWidth; // force reflow so the restart registers
          toggle.classList.add("is-stretching");
        }
        toggle.style.setProperty("--segment-index", String(nextIdx));
        options.forEach((o, i) => {
          const selected = i === nextIdx;
          o.setAttribute("aria-pressed", selected ? "true" : "false");
          o.setAttribute("aria-checked", selected ? "true" : "false");
          o.tabIndex = selected ? 0 : -1;
        });
        if (changed && typeof onChange === "function") {
          onChange(toggle.dataset.name, opt.dataset.value);
        }
      };

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
        toggle.style.setProperty("--segment-index", String(xToIndex(e.clientX)));
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
        opt.setAttribute("role", "radio");
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
          } else if (e.key === " " || e.key === "Enter") {
            e.preventDefault();
            activate(i);
          }
        });
      });
      toggle.addEventListener("animationend", (e) => {
        if (e.animationName === "segment-stretch") toggle.classList.remove("is-stretching");
      });

      const startIdx = options.findIndex((o) => o.dataset.value === toggle.dataset.value);
      activate(startIdx >= 0 ? startIdx : 0, { animate: false });
    });
  }

  function segmentValue(name, fallback, root) {
    const host = $(`.segment-toggle[data-name="${name}"]`, root);
    if (!host) return fallback;
    if (host.dataset && host.dataset.value) return host.dataset.value;
    const pressed = $('.segment-option[aria-pressed="true"]', host);
    return pressed ? pressed.dataset.value : fallback;
  }

  // ─── Small DOM builders shared by the views ───────────────────
  function fillSelect(select, entries) {
    if (!select) return;
    select.innerHTML = "";
    for (const entry of entries) {
      const opt = document.createElement("option");
      opt.value = entry.value;
      opt.textContent = entry.label;
      select.appendChild(opt);
    }
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
      panel.hidden = true;
      return;
    }
    const table = document.createElement("table");
    table.className = "patients-table";
    const head = document.createElement("tr");
    for (const heading of ["Patient", "Date of birth", "Visits", "Notes"]) {
      const th = document.createElement("th");
      th.textContent = heading;
      head.appendChild(th);
    }
    table.appendChild(head);
    for (const p of patients) {
      const tr = document.createElement("tr");
      for (const value of [p.display_name || "—", p.birth_date || "—", String(p.encounters), String(p.documents)]) {
        const td = document.createElement("td");
        td.textContent = value; // textContent: patient data as text, never markup
        tr.appendChild(td);
      }
      table.appendChild(tr);
    }
    body.appendChild(table);
    panel.hidden = false;
  }

  function clearPatients(panel, body) {
    if (body) body.innerHTML = "";
    if (panel) panel.hidden = true;
  }

  // `summaryId` is the run's OWN id (from its `done` event), so a rapid second
  // run cannot replace the detail this run is about to show. Advisory: a
  // failure never blocks the run roll-up.
  async function loadPatients(panel, body, summaryId) {
    if (!hasApi()) return;
    try {
      const res = await window.pywebview.api.last_run_summary(summaryId);
      if (res && res.ok) renderPatients(panel, body, res.patients || []);
    } catch (_) {
      /* advisory only */
    }
  }

  // ─── The shared run form ──────────────────────────────────────
  // Charts owns the plain rebuild; Migrate composes the same component beside a
  // destination picker. There is exactly one implementation of these fields —
  // the two views differ only in `mode`.
  function makeField(id, labelText, helpText, control) {
    const field = document.createElement("div");
    field.className = "field";
    const label = document.createElement("label");
    label.setAttribute("for", id);
    label.textContent = labelText;
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

  function makeInput(id, placeholder) {
    const input = document.createElement("input");
    input.type = "text";
    input.id = id;
    if (placeholder) input.placeholder = placeholder;
    return input;
  }

  // A select ships inside its wrapper: the chevron is drawn on the wrapper,
  // because a <select> takes no pseudo-element of its own.
  function makeSelect(id) {
    const wrap = document.createElement("div");
    wrap.className = "select-wrap";
    const select = document.createElement("select");
    select.id = id;
    wrap.appendChild(select);
    return wrap;
  }

  function makeToggle(id, labelText) {
    const label = document.createElement("label");
    label.className = "toggle";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.id = id;
    const track = document.createElement("span");
    track.className = "track";
    const text = document.createElement("span");
    text.textContent = labelText;
    label.appendChild(input);
    label.appendChild(track);
    label.appendChild(text);
    return label;
  }

  function makeSegment(name, labelText, helpText, options) {
    const row = document.createElement("div");
    row.className = "segment-row";
    const caption = document.createElement("span");
    caption.className = "field-help";
    caption.textContent = labelText;
    const toggle = document.createElement("div");
    toggle.className = "segment-toggle";
    toggle.setAttribute("role", "radiogroup");
    toggle.setAttribute("aria-label", labelText);
    toggle.dataset.name = name;
    toggle.dataset.value = options[0].value;
    const goo = document.createElement("div");
    goo.className = "segment-goo";
    goo.setAttribute("aria-hidden", "true");
    const indicator = document.createElement("div");
    indicator.className = "segment-indicator";
    goo.appendChild(indicator);
    toggle.appendChild(goo);
    for (const opt of options) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "segment-option";
      btn.dataset.value = opt.value;
      btn.textContent = opt.label;
      toggle.appendChild(btn);
    }
    row.appendChild(toggle);
    row.appendChild(caption);
    const wrap = document.createElement("div");
    wrap.className = "stack";
    wrap.appendChild(row);
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
        makeInput(id("export-dir"), "C:\\Users\\you\\Downloads\\ehr-export")
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
        migrate
          ? "Which system this export came from. Detect fills this in for you."
          : "Which system this export came from. 'Detect' works for all built-in formats.",
        makeSelect(id("source"))
      )
    );
    host.appendChild(
      makeField(
        id("out-dir"),
        "Where results go",
        "The folder Anastomosis writes the finished charts into.",
        makeInput(id("out-dir"), "C:\\Users\\you\\Documents\\anastomosis-charts")
      )
    );
    host.appendChild(
      migrate
        ? makeField(
            id("render"),
            "Chart pages",
            "Rendered pages (PDF) or data only.",
            makeSelect(id("render"))
          )
        : makeField(
            id("pack"),
            "Chart layout",
            "How the finished chart pages are laid out.",
            makeSelect(id("pack"))
          )
    );

    const matrix = document.createElement("div");
    matrix.className = "section-matrix";
    matrix.id = id("sections");
    host.appendChild(makeField(id("sections"), "Chart sections", "Which sections each chart page includes.", matrix));

    host.appendChild(
      makeSegment(
        "qa",
        "Double-check results",
        "Re-reads every finished chart and confirms names, dates, and values landed on the right patient.",
        [
          { value: "on", label: "On" },
          { value: "off", label: "Off" },
        ]
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
        makeInput(id("pack-dir"), "C:\\Users\\you\\Documents\\layouts")
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
          qa: segmentValue("qa", "on", host) === "on",
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

    for (const cell of cells) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "calendar-cell";
      btn.setAttribute("role", "gridcell");
      const iso = isoDate(cell.y, cell.m, cell.d);
      btn.dataset.iso = iso;
      const label = document.createElement("span");
      label.textContent = String(cell.d);
      btn.appendChild(label);
      if (cell.outside) btn.classList.add("calendar-cell--outside");
      if (iso === todayIso) btn.classList.add("calendar-cell--today");
      if (!cell.outside) {
        const hit = histogram[iso];
        if (hit) {
          const pending = hit.pending || 0;
          const done = hit.done || 0;
          const errors = hit.errors || 0;
          btn.classList.add("calendar-cell--has-data");
          if (errors > 0) btn.classList.add("calendar-cell--halo-errors");
          else if (pending > 0) btn.classList.add("calendar-cell--halo-pending");
          else if (done > 0) btn.classList.add("calendar-cell--halo-done");
          const sum = pending + done + errors;
          if (sum > 1) {
            const badge = document.createElement("span");
            badge.className = "calendar-count-badge";
            badge.textContent = sum > 99 ? "99+" : String(sum);
            btn.appendChild(badge);
          }
        }
      }
      grid.appendChild(btn);
    }
  }

  // ─── Tabs (one workspace, several modes) ─────────────────────
  // Generic chrome: every [role="tab"] in a .mode-tabs group shows the panel
  // named by its aria-controls and hides its siblings'.
  function initTabs(root) {
    for (const group of $$(".mode-tabs", root || document)) {
      const tabs = $$('[role="tab"]', group);
      const show = (chosen) => {
        for (const tab of tabs) {
          const selected = tab === chosen;
          tab.setAttribute("aria-selected", selected ? "true" : "false");
          const panel = el(tab.getAttribute("aria-controls"));
          if (panel) panel.hidden = !selected;
        }
      };
      for (const tab of tabs) tab.addEventListener("click", () => show(tab));
    }
  }

  // ─── About popover ────────────────────────────────────────────
  function initAbout() {
    const btn = el("about-btn");
    const popover = el("about-popover");
    if (!btn || !popover) return;
    const close = () => {
      popover.hidden = true;
      btn.setAttribute("aria-expanded", "false");
    };
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = popover.hidden;
      popover.hidden = !open;
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("mousedown", (e) => {
      if (popover.hidden) return;
      if (popover.contains(e.target) || btn.contains(e.target)) return;
      close();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") close();
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
    initTabs(document);
    for (const btn of $$("[data-view-target]")) {
      btn.addEventListener("click", () => showView(btn.dataset.viewTarget));
    }
    const band = el("view-band");
    const first = VIEWS[CURRENT];
    if (band && first) band.textContent = first.title || CURRENT;
    const strip = el("log-strip");
    if (strip) strip.addEventListener("click", toggleLogDrawer);
    const closeBtn = el("log-drawer-close");
    if (closeBtn) closeBtn.addEventListener("click", closeLogDrawer);
    document.addEventListener("mousedown", (e) => {
      const drawer = el("log-drawer");
      if (!drawer || drawer.hidden) return;
      if (drawer.contains(e.target) || (strip && strip.contains(e.target))) return;
      closeLogDrawer();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "l" && e.key !== "L") return;
      const tag = (document.activeElement && document.activeElement.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      e.preventDefault();
      toggleLogDrawer();
    });
    boot();
  }

  window.AnastShell = {
    hasApi,
    onReady,
    onInfo,
    icon,
    paintIcons,
    registerView,
    registerFlow,
    showView,
    currentView: () => CURRENT,
    logEvent,
    openLogDrawer,
    closeLogDrawer,
    toggleLogDrawer,
    showBanner,
    hideBanner,
    stageLabel,
    initSegmentToggles,
    segmentValue,
    fillSelect,
    renderSectionMatrix,
    gatherSections,
    buildRunForm,
    renderPatients,
    clearPatients,
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
