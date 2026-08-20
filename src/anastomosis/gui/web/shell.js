/*
 * Anastomosis GUI — shared shell interactions (carried from the predecessor).
 *
 * The Liquid Glass interaction patterns the predecessor hand-built, re-typed
 * and trimmed to the four pages we ship: the iOS-26 gooey segment toggle (with
 * pointer-drag + stretch physics), the Raycast-style command palette
 * (navigation + scoring), the activity log strip + drawer, and the calendar
 * grid builder (halo cells + count badges). Exposed as `window.AnastShell` so
 * each page's own script can wire them to OUR controller seam.
 *
 * No frameworks, no build step. PHI discipline: nothing in here ships a value —
 * the calendar paints counts, the palette paints whatever ids its host feeds
 * it, the log strip paints whatever PHI-free text its host hands `logEvent`.
 * The host pages never pass it a patient-derived value.
 */
"use strict";

(function () {
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function prefersReduced() {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  // ─── Liquid physics segment toggles (iOS 26 style) ────────────
  // Wires every .segment-toggle on the page. Each toggle tracks:
  //   data-name        — logical group name
  //   data-value       — currently selected option value
  //   --segment-index  — CSS var driving the coral indicator (float while
  //                      dragging, integer at rest)
  //   --segment-from   — previous index, used by the stretch keyframe
  // Interaction model: click / arrow-keys snap with the scaleX(1.45) stretch;
  // pointer-down + drag follows the cursor in real time, then snaps to the
  // closest slot on release. The gooey filter on .segment-goo smears the blob
  // edges mid-slide so it reads as a single elastic bubble.
  function initSegmentToggles(root, onChange) {
    $$(".segment-toggle", root).forEach((toggle) => {
      const options = $$(".segment-option", toggle);
      if (!options.length) return;
      const count = options.length;

      // --segment-count drives the indicator's slot width in app.css. It MUST be
      // set here, not as a `style="--segment-count: 2"` markup attribute: the
      // pages ship a strict `style-src 'self'` CSP with no 'unsafe-inline', so
      // the browser REFUSES an inline style attribute (console error + the var
      // never lands, leaving the indicator at width:0). CSSOM writes like this
      // one are not inline styles and are unaffected by the policy.
      toggle.style.setProperty("--segment-count", String(count));

      const activate = (nextIdxRaw, opts) => {
        const animate = !opts || opts.animate !== false;
        const nextIdx = Math.max(0, Math.min(count - 1, Math.round(nextIdxRaw)));
        const fromFloat = parseFloat(toggle.style.getPropertyValue("--segment-index") || "0");
        const fromIdx = Math.round(fromFloat);
        const opt = options[nextIdx];
        if (!opt) return;

        // Early-out only when re-activating the same slot AND still animating
        // AND not coming off a drag (float != int). Init calls always run.
        const sameSlot = nextIdx === fromIdx && Math.abs(fromFloat - fromIdx) < 0.001;
        if (animate && sameSlot) return;

        const changed = opt.dataset.value !== toggle.dataset.value;
        toggle.dataset.value = opt.dataset.value;

        // Trigger the stretch keyframe on a real slot-change with motion on.
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

      // ── Drag gesture state — pointer X mapped to a fractional index ──
      const drag = { active: false, pointerId: null, startX: 0, startIdx: 0, moved: false, slotWidth: 0, originX: 0 };

      const measure = () => {
        const rect = toggle.getBoundingClientRect();
        drag.slotWidth = (rect.width - 8) / count; // 8px = 2×4px padding
        drag.originX = rect.left + 4;
      };
      const xToIndex = (clientX) => {
        const x = clientX - drag.originX;
        const idx = x / drag.slotWidth - 0.5; // pointer at slot center → that index
        return Math.max(0, Math.min(count - 1, idx));
      };

      const onPointerDown = (e) => {
        if (e.button !== undefined && e.button !== 0) return;
        drag.active = true;
        drag.pointerId = e.pointerId;
        drag.moved = false;
        drag.startX = e.clientX;
        measure();
        drag.startIdx = parseFloat(toggle.style.getPropertyValue("--segment-index") || "0");
        try { toggle.setPointerCapture(e.pointerId); } catch (_) { /* capture optional */ }
      };
      const onPointerMove = (e) => {
        if (!drag.active || e.pointerId !== drag.pointerId) return;
        const dx = Math.abs(e.clientX - drag.startX);
        if (!drag.moved && dx < 4) return; // 4px deadzone before committing to a drag
        if (!drag.moved) {
          drag.moved = true;
          toggle.classList.add("is-dragging");
          toggle.classList.remove("is-stretching");
        }
        toggle.style.setProperty("--segment-index", String(xToIndex(e.clientX)));
        e.preventDefault();
      };
      const finishDrag = (e, opts) => {
        const canceled = opts && opts.canceled;
        if (!drag.active || (e && e.pointerId !== drag.pointerId)) return;
        const wasMoved = drag.moved;
        drag.active = false;
        drag.moved = false;
        try { toggle.releasePointerCapture(drag.pointerId); } catch (_) { /* release optional */ }
        drag.pointerId = null;
        if (!wasMoved) {
          toggle.classList.remove("is-dragging");
          // A plain tap, and it must be resolved HERE. pointerdown took pointer
          // capture on the TOGGLE, and the browser then retargets the following
          // click to the capture element — so the per-option click listener
          // below never fires for a real mouse/touch press, and the toggle
          // would look dead to anything but the keyboard. Activate the slot
          // under the pointer instead. The option listener still serves
          // SYNTHETIC clicks (the command palette's setSegment → btn.click(),
          // which produces no pointer sequence at all), and activate() early-
          // outs on an unchanged slot, so the two paths cannot double-fire.
          if (!canceled && e && typeof e.clientX === "number") activate(xToIndex(e.clientX));
          return;
        }
        const finalFloat = parseFloat(toggle.style.getPropertyValue("--segment-index") || "0");
        const target = canceled ? Math.round(drag.startIdx) : Math.round(finalFloat);
        toggle.classList.remove("is-dragging");
        void toggle.offsetWidth; // reflow so the snap transition kicks in
        activate(target, { animate: false });
      };

      toggle.addEventListener("pointerdown", onPointerDown);
      toggle.addEventListener("pointermove", onPointerMove);
      toggle.addEventListener("pointerup", finishDrag);
      toggle.addEventListener("pointercancel", (e) => finishDrag(e, { canceled: true }));

      options.forEach((opt, i) => {
        opt.setAttribute("role", "radio");
        opt.addEventListener("click", (e) => {
          if (drag.moved) { e.preventDefault(); e.stopPropagation(); return; }
          activate(i);
        });
        opt.addEventListener("keydown", (e) => {
          if (e.key === "ArrowRight" || e.key === "ArrowDown") {
            e.preventDefault();
            const next = (i + 1) % count;
            activate(next); options[next].focus();
          } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
            e.preventDefault();
            const prev = (i - 1 + count) % count;
            activate(prev); options[prev].focus();
          } else if (e.key === "Home") {
            e.preventDefault(); activate(0); options[0].focus();
          } else if (e.key === "End") {
            e.preventDefault(); activate(count - 1); options[count - 1].focus();
          } else if (e.key === " " || e.key === "Enter") {
            e.preventDefault(); activate(i);
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

  // ─── Command palette (Raycast-style ⌘K / Ctrl+K) ──────────────
  // Host passes an array of commands: {id, label, hint, icon, disabled?, action}.
  // Returns a controller with open/close/toggle; the host owns Ctrl+K binding.
  function commandScore(cmd, query) {
    if (!query) return 0;
    const q = query.toLowerCase();
    const label = cmd.label.toLowerCase();
    const hint = (cmd.hint || "").toLowerCase();
    if (label.startsWith(q)) return 100 - (label.length - q.length);
    if (label.includes(q)) return 60 - (label.length - q.length);
    if (hint.includes(q)) return 30;
    let qi = 0; // subsequence fallback
    for (let i = 0; i < label.length && qi < q.length; i++) {
      if (label[i] === q[qi]) qi++;
    }
    return qi === q.length ? 10 : -1;
  }

  function initCommandPalette(commands) {
    const overlay = $("#cmd-palette");
    const input = $("#cmd-palette-search");
    const list = $("#cmd-palette-list");
    if (!overlay || !input || !list) return { open() {}, close() {}, toggle() {}, isOpen: () => false };

    const st = { open: false, items: [], selected: 0 };

    const filter = (query) =>
      commands
        .map((cmd) => ({ cmd, score: commandScore(cmd, query) }))
        .filter((x) => x.score >= 0)
        .sort((a, b) => b.score - a.score)
        .map((x) => x.cmd);

    const render = () => {
      list.innerHTML = "";
      if (st.items.length === 0) {
        const empty = document.createElement("li");
        empty.className = "cmd-palette-empty";
        empty.textContent = "no commands match";
        list.appendChild(empty);
        return;
      }
      st.items.forEach((cmd, idx) => {
        const li = document.createElement("li");
        li.className = "cmd-palette-item";
        li.setAttribute("role", "option");
        li.dataset.cmdId = cmd.id;
        li.setAttribute("aria-selected", idx === st.selected ? "true" : "false");
        if (cmd.disabled && cmd.disabled()) {
          li.setAttribute("aria-disabled", "true");
          li.style.opacity = "0.4";
        }
        if (cmd.icon) {
          const iconWrap = document.createElement("span");
          iconWrap.className = "cmd-palette-item-icon";
          iconWrap.innerHTML = cmd.icon; // trusted static SVG strings from the host
          li.appendChild(iconWrap);
        }
        const label = document.createElement("span");
        label.className = "cmd-palette-item-label";
        label.textContent = cmd.label;
        const hint = document.createElement("span");
        hint.className = "cmd-palette-item-hint";
        hint.textContent = cmd.hint || "";
        li.appendChild(label);
        li.appendChild(hint);
        li.addEventListener("mouseenter", () => { st.selected = idx; updateSel(); });
        li.addEventListener("click", () => execute());
        list.appendChild(li);
      });
    };

    const updateSel = () => {
      $$("[role=option]", list).forEach((li, i) =>
        li.setAttribute("aria-selected", i === st.selected ? "true" : "false")
      );
      const sel = $('[aria-selected="true"]', list);
      if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: "nearest" });
    };

    const execute = () => {
      const cmd = st.items[st.selected];
      if (!cmd) return;
      if (cmd.disabled && cmd.disabled()) return;
      close();
      try { cmd.action(); } catch (_) { /* a command's side-effect must not crash the palette */ }
    };

    function open() {
      overlay.hidden = false;
      st.open = true;
      st.selected = 0;
      input.value = "";
      st.items = filter("");
      render();
      requestAnimationFrame(() => input.focus());
    }
    function close() { overlay.hidden = true; st.open = false; }
    function toggle() { if (st.open) close(); else open(); }

    input.addEventListener("input", () => {
      st.items = filter(input.value);
      st.selected = 0;
      render();
    });
    input.addEventListener("keydown", (e) => {
      if (!st.open) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (st.items.length) { st.selected = (st.selected + 1) % st.items.length; updateSel(); }
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (st.items.length) { st.selected = (st.selected - 1 + st.items.length) % st.items.length; updateSel(); }
      } else if (e.key === "Enter") {
        e.preventDefault(); execute();
      } else if (e.key === "Escape") {
        e.preventDefault(); close();
      }
    });
    overlay.addEventListener("click", (e) => {
      if (e.target.dataset && e.target.dataset.cmdDismiss === "true") close();
    });

    return { open, close, toggle, isOpen: () => st.open };
  }

  // ─── Activity log strip + drawer ──────────────────────────────
  const MAX_LOG_ROWS = 200;

  function fmtTime(d) {
    const pad = (n) => n.toString().padStart(2, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  // Host calls logEvent({kind, glyph, msg}) — kind ∈ ok|warn|error|info. The
  // msg is whatever PHI-free text the host built (stage names, counts, type
  // names). Updates the always-visible strip AND appends to the drawer ring.
  function logEvent(entry) {
    const kind = entry.kind || "info";
    const glyph = entry.glyph || GLYPH[kind] || "·";
    const msg = entry.msg == null ? "" : String(entry.msg);
    const ts = fmtTime(new Date());

    const strip = $("#log-strip");
    if (strip) {
      strip.dataset.kind = kind;
      const sTs = $("#log-strip-ts"); if (sTs) sTs.textContent = ts;
      const sGlyph = $("#log-strip-glyph"); if (sGlyph) sGlyph.textContent = glyph;
      const sMsg = $("#log-strip-msg"); if (sMsg) sMsg.textContent = msg;
    }

    const rows = $("#log-rows");
    if (!rows) {
      // Pages without the activity strip (the wizard and the two learn wizards)
      // would otherwise SWALLOW the entry entirely — including the shell's
      // close-barrier notice, which is the operator's only explanation for a
      // window close that was vetoed mid-run. Fall back to the page banner for
      // the entries that carry a problem; informational chatter stays dropped.
      if (kind === "error" || kind === "warn") bannerFallback(msg);
      return;
    }
    const row = document.createElement("div");
    row.className = `log-row log-row--${kind}`;
    const tsEl = document.createElement("span"); tsEl.className = "log-ts"; tsEl.textContent = ts;
    const g = document.createElement("span"); g.className = "log-glyph"; g.textContent = glyph;
    const m = document.createElement("span"); m.className = "log-msg"; m.textContent = msg;
    row.appendChild(tsEl); row.appendChild(g); row.appendChild(m);
    rows.appendChild(row);
    while (rows.childElementCount > MAX_LOG_ROWS) rows.removeChild(rows.firstChild);
    const drawer = $("#log-drawer");
    if (drawer && !drawer.hidden) {
      const atBottom = rows.scrollHeight - rows.scrollTop - rows.clientHeight < 20;
      if (atBottom) rows.scrollTop = rows.scrollHeight;
    }
  }

  const GLYPH = { ok: "✓", warn: "⚠", error: "✗", info: "·" };

  // The log strip's stand-in on pages that don't host one: every page ships a
  // #banner, so a problem entry still reaches the operator. textContent (never
  // innerHTML) — the host's text is PHI-free but never trusted as markup.
  function bannerFallback(msg) {
    const banner = $("#banner");
    if (!banner) return;
    banner.textContent = msg;
    banner.classList.add("show");
  }

  function openLogDrawer() {
    const drawer = $("#log-drawer"); const strip = $("#log-strip");
    if (!drawer) return;
    drawer.hidden = false;
    if (strip) strip.setAttribute("aria-expanded", "true");
    const rows = $("#log-rows"); if (rows) rows.scrollTop = rows.scrollHeight;
  }
  function closeLogDrawer() {
    const drawer = $("#log-drawer"); const strip = $("#log-strip");
    if (!drawer) return;
    drawer.hidden = true;
    if (strip) strip.setAttribute("aria-expanded", "false");
  }
  function toggleLogDrawer() {
    const drawer = $("#log-drawer");
    if (!drawer) return;
    if (drawer.hidden) openLogDrawer(); else closeLogDrawer();
  }
  function initLogStrip() {
    const strip = $("#log-strip");
    const closeBtn = $("#log-drawer-close");
    if (strip) strip.addEventListener("click", toggleLogDrawer);
    if (closeBtn) closeBtn.addEventListener("click", closeLogDrawer);
    document.addEventListener("mousedown", (e) => {
      const drawer = $("#log-drawer");
      if (!drawer || drawer.hidden) return;
      if (drawer.contains(e.target)) return;
      if (strip && strip.contains(e.target)) return;
      closeLogDrawer();
    });
  }

  // ─── Controller bridge helpers (shared by the run pages) ──────
  // The dashboard and the migration wizard drive different flows but present
  // their results the same way, so the presentation lives here once. PHI rule
  // (unchanged): the per-patient detail below is fetched over the bridge and
  // painted with textContent for LOCAL display only — it never rides an event
  // and is never logged.

  function hasApi() {
    return typeof window.pywebview !== "undefined" && !!window.pywebview.api;
  }

  // Run a page's bootstrap now AND again if the bridge lands late.
  //
  // pywebview injects `window.pywebview.api` asynchronously and announces it
  // with a `pywebviewready` window event, so a page that only probes hasApi()
  // at DOMContentLoaded can lose the race and paint its "launch via anast gui"
  // notice over a bridge that is about to arrive — permanently, because nothing
  // re-runs the bootstrap. Every page bootstraps through here instead: `boot`
  // runs immediately (painting the offline notice when there is genuinely no
  // bridge — the plain-browser preview) and ONCE more when `pywebviewready`
  // fires, at which point the page's own populate clears the notice. A page
  // that already has the bridge does not re-register: the event is either
  // already past or would only repeat work.
  function onApiReady(boot) {
    boot();
    if (hasApi()) return;
    window.addEventListener("pywebviewready", () => boot(), { once: true });
  }

  // Fill a <select> with [{value, label}] entries, replacing what was there.
  // Each page supplies its own leading entry (the dashboard's "auto-detect"
  // sentinel vs the wizard's "Select a source…" prompt are different contracts).
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

  // Build the section-selection checkbox matrix into `matrix` from a
  // {key: {label, default}} map. The host owns WHICH map applies (the dashboard
  // keys off the chosen pack, the wizard off the render mode) and the
  // empty-state wording; the toggle markup is identical on both pages.
  function renderSectionMatrix(matrix, sections, emptyText) {
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
      input.checked = flag.default !== false;
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

  // Read a section matrix back into the {section: bool} map the run calls take.
  // Scoped to inputs carrying data-section so a future non-section checkbox in
  // the same container can never pollute the map.
  function gatherSections(matrix) {
    const sections = {};
    if (!matrix) return sections;
    for (const box of $$("input[data-section]", matrix)) {
      sections[box.dataset.section] = box.checked;
    }
    return sections;
  }

  // The per-patient roll-up table (patient / dob / encounters / notes).
  // `panel` hides itself when there is nothing to show.
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

  function clearPatients(panel, body) {
    if (body) body.innerHTML = "";
    if (panel) panel.hidden = true;
  }

  // Fetch a finished run's per-patient detail and paint it. `summaryId` is the
  // run's OWN id (from its `done` event), so a rapid second run cannot replace
  // the detail this run is about to show (the summary race). The summary is
  // advisory: a failure never blocks the run roll-up.
  async function loadPatients(panel, body, summaryId) {
    if (!hasApi()) return;
    try {
      const res = await window.pywebview.api.last_run_summary(summaryId);
      if (res && res.ok) renderPatients(panel, body, res.patients || []);
    } catch (_) {
      /* advisory only */
    }
  }

  // ─── Calendar grid builder (halo cells + count badges) ────────
  const MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
  ];
  const isoDate = (y, m, d) =>
    `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
  const daysInMonth = (year, monthIdx) => new Date(year, monthIdx + 1, 0).getDate();

  // Renders a Mon-first month grid into `gridEl`/`titleEl`. `histogram` is a
  // map of ISO date → {pending, done, errors} (counts only). The halo colour
  // follows the original priority: errors > pending > done. A total > 1 paints
  // the count badge. `onPick(iso)` (optional) fires on an in-month click.
  function renderCalendar(opts) {
    const grid = opts.gridEl;
    const title = opts.titleEl;
    const year = opts.year;
    const month = opts.month;
    const histogram = opts.histogram || {};
    if (!grid) return;
    if (title) title.textContent = `${MONTH_NAMES[month]} ${year}`;
    grid.innerHTML = "";

    const firstOfMonth = new Date(year, month, 1);
    const leading = (firstOfMonth.getDay() + 6) % 7; // shift Sun-first → Mon-first
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

    cells.forEach((cell) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "calendar-cell";
      btn.setAttribute("role", "gridcell");
      const iso = isoDate(cell.y, cell.m, cell.d);
      btn.dataset.iso = iso;
      const labelSpan = document.createElement("span");
      labelSpan.textContent = String(cell.d);
      btn.appendChild(labelSpan);
      if (cell.outside) btn.classList.add("calendar-cell--outside");
      if (iso === todayIso) btn.classList.add("calendar-cell--today");

      if (!cell.outside) {
        const hit = histogram[iso];
        if (hit) {
          const pending = hit.pending || 0;
          const done = hit.done || 0;
          const errors = hit.errors || 0;
          const t = pending + done + errors;
          btn.classList.add("calendar-cell--has-data");
          if (errors > 0) btn.classList.add("calendar-cell--halo-errors");
          else if (pending > 0) btn.classList.add("calendar-cell--halo-pending");
          else if (done > 0) btn.classList.add("calendar-cell--halo-done");
          if (t > 1) {
            const badge = document.createElement("span");
            badge.className = "calendar-count-badge";
            badge.textContent = t > 99 ? "99+" : String(t);
            btn.appendChild(badge);
          }
        }
        if (typeof opts.onPick === "function") {
          btn.addEventListener("click", () => opts.onPick(iso, hit || null));
        }
      }
      grid.appendChild(btn);
    });
  }

  window.AnastShell = {
    hasApi,
    onApiReady,
    initSegmentToggles,
    segmentValue,
    initCommandPalette,
    initLogStrip,
    logEvent,
    openLogDrawer,
    closeLogDrawer,
    toggleLogDrawer,
    renderCalendar,
    fillSelect,
    renderSectionMatrix,
    gatherSections,
    renderPatients,
    clearPatients,
    loadPatients,
    MONTH_NAMES,
  };
})();
