/*
 * Anastomosis GUI — Teach, mode 2: teach an export format from one example.
 *
 * Owns the "source_init" flow; the Teach view itself is registered by
 * packgen.js (one workspace, two modes, the same two-step shape).
 *
 *   1. "Look at the example" calls source_init_async(confirmed=false) — the
 *      controller refuses to write and stashes the proposed match-up;
 *   2. that proposal IS the edit surface. Three grouping controls above the
 *      table, a destination and a way-of-reading below it per column. There is
 *      no second screen and no correct-then-review stage: Save is Revalidate,
 *      and the corrections ride the call as its `review` argument;
 *   3. the confirmation enables "Save this format", which calls
 *      source_init_async(confirmed=true, review): it proves no column would be
 *      lost, then stores the format for this user account only.
 *
 * `review` stays null whenever the operator changed nothing, so an untouched
 * proposal takes exactly the path it always took.
 *
 * Consent is per-analysis AND per-edit: any change to any control un-ticks the
 * confirmation, because a tick made before a change never looked at what the
 * click is about to save.
 *
 * PHI discipline: the proposal carries column NAMES, inferred type labels,
 * counts and masked shapes only — never a cell value. Everything this file
 * paints is built from those, from the closed vocabularies below, or from a
 * literal the operator typed themselves. That includes the refusal sentence:
 * it is composed HERE, from the same label tables that fill the choosers, so
 * it cannot drift into different words than the control it points at.
 */
"use strict";

(function () {
  const Shell = window.AnastShell;
  const el = (id) => document.getElementById(id);

  const REVIEW_STEP = "Step 2 of 2 — review, correct anything wrong, and confirm.";

  // ─── The closed vocabularies ──────────────────────────────────
  //
  // Hardcoded, and that is the point: a learned mapping may only name a verb
  // from `sources/learned/transforms.py`'s fixed table, so the set the operator
  // picks from is closed by design. A verb added there is a deliberate act that
  // comes here too, with a sentence a physician can read.

  //: The permanent first entry of every column's destination chooser. Nothing
  //: is ever dropped, so "not mapped" is a home, not a loss.
  const KEEP_UNMAPPED = { value: "", label: "Keep as extra data (not mapped)" };

  //: The permanent first entry of the visit-column chooser: the answer that
  //: says there is no such column, which is a real answer and not a blank.
  const NO_VISIT_COLUMN = {
    value: "",
    label: "No separate visit column — treat each row as its own visit",
  };

  const ROW_SCOPES = [
    { value: "patient", label: "One patient per row", note: "Every row is a different patient." },
    {
      value: "encounter",
      label: "One visit per row",
      note: "The same patient can appear on several rows.",
    },
  ];

  //: A trailing colon marks the verbs that take an argument: the spec string is
  //: this value with the argument appended, which is exactly what
  //: `parse_transform` splits back apart.
  const TRANSFORMS = [
    { value: "strip", label: "Trim extra spaces (the usual choice)" },
    { value: "identity", label: "Use the value exactly as it is" },
    { value: "parse_date", label: "Read as a date (common formats)" },
    { value: "parse_datetime", label: "Read as a date and time (common formats)" },
    { value: "parse_date:", label: "Read as a date in one set pattern" },
    { value: "parse_datetime:", label: "Read as a date and time in one set pattern" },
    { value: "phone", label: "Tidy up a phone number" },
    { value: "numeric", label: "Read as a number" },
    { value: "upper", label: "Make it all capital letters" },
    { value: "lower", label: "Make it all lower case" },
    { value: "split:", label: "Take one piece of a value that has separators" },
    { value: "const:", label: "Always write the same wording" },
  ];

  //: Date patterns as a chooser, never a text box: a pattern typed by hand
  //: fails at load time, in front of a person who cannot see why. Each is
  //: labelled with the same made-up date written that way — a specimen, never
  //: a cell from the file.
  const DATE_PATTERNS = [
    { value: "%m/%d/%Y", label: "07/22/2024" },
    { value: "%d/%m/%Y", label: "22/07/2024" },
    { value: "%Y-%m-%d", label: "2024-07-22" },
    { value: "%m-%d-%Y", label: "07-22-2024" },
    { value: "%Y%m%d", label: "20240722" },
    { value: "%m/%d/%y", label: "07/22/24" },
  ];

  const DATETIME_PATTERNS = [
    { value: "%m/%d/%Y %H:%M", label: "07/22/2024 14:30" },
    { value: "%d/%m/%Y %H:%M", label: "22/07/2024 14:30" },
    { value: "%Y-%m-%d %H:%M", label: "2024-07-22 14:30" },
    { value: "%Y-%m-%d %H:%M:%S", label: "2024-07-22 14:30:00" },
    { value: "%m/%d/%Y %I:%M %p", label: "07/22/2024 02:30 PM" },
    { value: "%Y%m%d%H%M%S", label: "20240722143000" },
  ];

  //: A colon is deliberately absent: `split:DELIM:INDEX` is split on its first
  //: colon, so a colon delimiter cannot be written down at all. Offering one
  //: would be offering a choice that always fails at load time.
  const DELIMITERS = [
    { value: ",", label: "Comma" },
    { value: "|", label: "Vertical bar" },
    { value: ";", label: "Semicolon" },
    { value: " ", label: "Space" },
    { value: "-", label: "Hyphen" },
  ];

  //: Positions by name, never a number box. `round_trip` proves losslessness
  //: for UNMAPPED columns only, so a mapped column that takes the wrong piece
  //: is wrong quietly and forever — "1" meaning the first piece is one keypress
  //: away, and nothing downstream would ever say so.
  const POSITIONS = [
    { value: "0", label: "1st piece" },
    { value: "1", label: "2nd piece" },
    { value: "2", label: "3rd piece" },
    { value: "-1", label: "Last piece" },
  ];

  //: What a key column is spoken for as. It is not unmatched — it is what the
  //: grouping above is keyed on — and the table used to tell a physician
  //: otherwise.
  const ROLE_LABEL = { patient: "Patient ID", encounter: "Visit ID" };

  //: Acronyms a physician reads in capitals. The shell's own list is built from
  //: vendor and pack ids, where these never occur; widening it there would
  //: change how every id in the app is printed, so the canonical FIELD names
  //: carry their own short list here.
  const FIELD_SHOUTED = new Map([
    ["mrn", "MRN"],
    ["prn", "PRN"],
    ["ssn", "SSN"],
  ]);

  // ─── Reading the vocabularies back ────────────────────────────

  function labelOf(entries, value) {
    const found = entries.find((entry) => entry.value === value);
    return found ? found.label : "";
  }

  function lowerFirst(text) {
    return text ? text.charAt(0).toLowerCase() + text.slice(1) : "";
  }

  //: `encounter.date_of_service` -> "Date Of Service". The dotted path itself
  //: rides the chooser row's mono caption and its tooltip (§10.11: a machine
  //: identifier is never a visible label).
  function targetLabel(path) {
    return String(path)
      .split(".")
      .slice(1)
      .map((part) =>
        part
          .split("_")
          .map(
            (word) =>
              FIELD_SHOUTED.get(word) || Shell.displayName(word.replace(/(\d+)/g, " $1"))
          )
          .join(" ")
      )
      .join(" ");
  }

  //: `parse_date` -> "parse_date"; `parse_date:%Y-%m-%d` -> "parse_date:".
  function verbOf(spec) {
    const text = String(spec || "");
    const head = text.split(":")[0];
    return text.length > head.length ? `${head}:` : head;
  }

  function argOf(spec) {
    const text = String(spec || "");
    const at = text.indexOf(":");
    return at < 0 ? "" : text.slice(at + 1);
  }

  //: `split`'s two arguments, cut at the FIRST colon exactly as the loader
  //: cuts them.
  function splitArgs(arg) {
    const at = String(arg).indexOf(":");
    return at < 0 ? [String(arg), POSITIONS[0].value] : [arg.slice(0, at), arg.slice(at + 1)];
  }

  //: The whole sentence for one transform spec, argument included — so two
  //: pickings of the same verb with different arguments never read alike.
  function transformLabel(spec) {
    const verb = verbOf(spec);
    const base = labelOf(TRANSFORMS, verb) || verb;
    const arg = argumentLabel(verb, argOf(spec));
    return arg ? `${base} (${arg})` : base;
  }

  function argumentLabel(verb, arg) {
    if (verb === "parse_date:") return labelOf(DATE_PATTERNS, arg);
    if (verb === "parse_datetime:") return labelOf(DATETIME_PATTERNS, arg);
    if (verb === "split:") {
      const parts = splitArgs(arg);
      return `${labelOf(DELIMITERS, parts[0])}, ${lowerFirst(labelOf(POSITIONS, parts[1]))}`;
    }
    return verb === "const:" ? arg : "";
  }

  //: The verb's spec string when it is first picked: the argument choosers open
  //: on their first entry, so the spec has to agree with them.
  function defaultSpec(verb) {
    if (verb === "parse_date:") return verb + DATE_PATTERNS[0].value;
    if (verb === "parse_datetime:") return verb + DATETIME_PATTERNS[0].value;
    if (verb === "split:") return `${verb}${DELIMITERS[0].value}:${POSITIONS[0].value}`;
    return verb;
  }

  //: The profiler's evidence for one column — the type it inferred and the
  //: letters-for-letters, digits-for-digits mask. This is the line that makes a
  //: wrong guess visible without a vocabulary lesson.
  function evidenceOf(suggestion) {
    return [suggestion.inferred_type, suggestion.sample_shape].filter(Boolean).join(" · ");
  }

  function shapeSentence(suggestion) {
    if (suggestion.inferred_type && suggestion.sample_shape) {
      return `${suggestion.inferred_type} shaped ${suggestion.sample_shape}`;
    }
    return suggestion.sample_shape || suggestion.inferred_type || "";
  }

  // ─── What is on screen ────────────────────────────────────────
  //
  // Two copies of the same two answers: what the scorer proposed, kept whole so
  // "what you changed" has something to diff against and so a column that stops
  // being a key falls back to what was proposed for it; and what the operator
  // currently says, which is what Save sends.

  let proposal = null;
  let columns = [];
  let targetChoices = [];
  let proposedPicks = new Map();
  let picks = new Map();
  let proposedGroup = null;
  let group = null;
  let visitTrigger = null;
  const rows = new Map();
  let pickSeq = 0;

  function nextId() {
    pickSeq += 1;
    return `format-pick-${pickSeq}`;
  }

  function hasApi() {
    return Shell.hasApi();
  }

  function setStep(text) {
    Shell.setStatus(el("format-step"), text);
  }

  //: A grouping key as one column name. The controller sends a name or null;
  //: an older payload sent a list of them.
  function keyValue(key) {
    if (Array.isArray(key)) return key.length ? String(key[0]) : "";
    return key ? String(key) : "";
  }

  function roleOf(column, grouping) {
    if (column === grouping.patient) return "patient";
    if (grouping.encounter && column === grouping.encounter) return "encounter";
    return "";
  }

  // ─── The review, and what changed ─────────────────────────────

  //: The wire shape the controller parses: the three grouping answers, and a
  //: COMPLETE (not sparse) set of column decisions. A column absent from it and
  //: not a key is unmapped-and-kept — which is the same thing the interpreter
  //: does with it, so the omission is the answer rather than a gap.
  function reviewOf(grouping, decisions) {
    const chosen = {};
    for (const column of columns) {
      if (roleOf(column, grouping)) continue;
      const pick = decisions.get(column);
      if (!pick || !pick.target) continue;
      chosen[column] = [pick.target, pick.transform];
    }
    return {
      patient_key: grouping.patient,
      encounter_key: grouping.encounter || null,
      row_scope: grouping.scope,
      decisions: chosen,
    };
  }

  function samePair(a, b) {
    if (!a || !b) return !a && !b;
    return a[0] === b[0] && a[1] === b[1];
  }

  function sameReview(a, b) {
    if (a.patient_key !== b.patient_key) return false;
    if (a.encounter_key !== b.encounter_key) return false;
    if (a.row_scope !== b.row_scope) return false;
    const named = Object.keys(a.decisions);
    if (named.length !== Object.keys(b.decisions).length) return false;
    return named.every((column) => samePair(a.decisions[column], b.decisions[column]));
  }

  //: What Save sends: null when the operator changed nothing, so an accepted
  //: proposal takes the path it always took, byte for byte.
  function currentReview() {
    if (!proposal) return null;
    const now = reviewOf(group, picks);
    return sameReview(now, reviewOf(proposedGroup, proposedPicks)) ? null : now;
  }

  function describeDecision(role, pair) {
    if (role) return `used as the ${lowerFirst(ROLE_LABEL[role])}`;
    if (!pair) return "kept as extra data, not mapped";
    return `going to ${targetLabel(pair[0])}, ${lowerFirst(transformLabel(pair[1]))}`;
  }

  //: One prose line per change, in the vocabulary the controls use. Rendered
  //: only when there is something to render, so the disclosure is never an
  //: empty promise.
  function changeLines() {
    const was = reviewOf(proposedGroup, proposedPicks);
    const now = reviewOf(group, picks);
    const lines = [];
    if (now.patient_key !== was.patient_key) {
      lines.push(`Patients are identified by ${now.patient_key} now, not ${was.patient_key}.`);
    }
    if (now.encounter_key !== was.encounter_key) {
      lines.push(
        now.encounter_key
          ? `Visits are identified by ${now.encounter_key} now, ` +
              (was.encounter_key
                ? `not ${was.encounter_key}.`
                : "instead of each row being its own visit.")
          : `Each row is its own visit now, instead of being grouped by ${was.encounter_key}.`
      );
    }
    if (now.row_scope !== was.row_scope) {
      lines.push(
        `Rows are read as ${lowerFirst(labelOf(ROW_SCOPES, now.row_scope))} now, ` +
          `not ${lowerFirst(labelOf(ROW_SCOPES, was.row_scope))}.`
      );
    }
    for (const column of columns) {
      const wasRole = roleOf(column, proposedGroup);
      const nowRole = roleOf(column, group);
      if (wasRole === nowRole && samePair(was.decisions[column], now.decisions[column])) continue;
      lines.push(
        `${column}: was ${describeDecision(wasRole, was.decisions[column])} — ` +
          `now ${describeDecision(nowRole, now.decisions[column])}.`
      );
    }
    return lines;
  }

  function renderChanges() {
    const lines = changeLines();
    const list = el("format-changes-list");
    list.textContent = "";
    for (const line of lines) {
      const paragraph = document.createElement("p");
      paragraph.textContent = line;
      list.appendChild(paragraph);
    }
    const box = el("format-changes");
    box.hidden = lines.length === 0;
    if (!lines.length) box.open = false;
  }

  // ─── Consent ──────────────────────────────────────────────────

  //: Every control on this panel ends here. Ticking the box is per-analysis and
  //: has never been sticky; an edit revokes it for the same reason — otherwise
  //: an operator ticks, keeps adjusting, and saves a match-up that particular
  //: click never looked at.
  function onEdit() {
    el("format-confirm").checked = false;
    el("format-save").disabled = true;
    clearAttention();
    Shell.hideBanner();
    paintScores();
    renderChanges();
  }

  // ─── The controls ─────────────────────────────────────────────

  function cell(row, className) {
    const span = document.createElement("span");
    span.setAttribute("role", "cell");
    if (className) span.className = className;
    row.appendChild(span);
    return span;
  }

  //: A chooser, filled and wired. `fillChooser` selects its first entry, so the
  //: value is set after it — with the setter, which does not re-announce.
  function makeChooser(id, entries, value, pick, onChange) {
    const wrap = Shell.makeChooser(id);
    const trigger = wrap.querySelector(".chooser-trigger");
    trigger.dataset.pick = pick;
    Shell.fillChooser(trigger, entries);
    trigger.value = value;
    trigger.addEventListener("change", () => onChange(trigger.value));
    return wrap;
  }

  //: A chooser with a visible label of its own — the grouping controls, and the
  //: argument a parametric transform needs.
  function addLabelledChooser(host, opts) {
    const id = opts.id || nextId();
    const wrap = makeChooser(id, opts.entries, opts.value, opts.pick, opts.onChange);
    if (opts.prose) wrap.classList.add("chooser--prose");
    host.appendChild(Shell.makeField(id, opts.label, "", wrap));
    return wrap.querySelector(".chooser-trigger");
  }

  //: A chooser inside a table cell. The column header says what the control is
  //: for, so its own name is for assistive technology only — and it names the
  //: column, which a shared header cannot.
  function addCellChooser(host, opts) {
    const id = nextId();
    const label = document.createElement("span");
    label.className = "sr-only";
    label.id = `${id}-label`;
    label.textContent = opts.spoken;
    host.appendChild(label);
    host.appendChild(makeChooser(id, opts.entries, opts.value, opts.pick, opts.onChange));
  }

  // ─── The grouping controls ────────────────────────────────────

  function columnEntries(exclude) {
    return columns
      .filter((column) => column !== exclude)
      .map((column) => ({ value: column, label: column }));
  }

  function visitEntries() {
    return [NO_VISIT_COLUMN].concat(columnEntries(group.patient));
  }

  function renderStructure() {
    const host = el("format-structure");
    host.textContent = "";
    addLabelledChooser(host, {
      id: "format-patient-key",
      label: "Which column identifies the patient",
      entries: columnEntries(""),
      value: group.patient,
      pick: "patient-key",
      onChange: onPatientKey,
    });
    visitTrigger = addLabelledChooser(host, {
      id: "format-visit-key",
      label: "Which column identifies the visit",
      entries: visitEntries(),
      value: group.encounter,
      pick: "visit-key",
      onChange: (value) => {
        group.encounter = value;
        renderTable();
        onEdit();
      },
    });
    addLabelledChooser(host, {
      id: "format-row-scope",
      label: "What one row of your file is",
      entries: ROW_SCOPES,
      value: group.scope,
      pick: "row-scope",
      prose: true,
      onChange: (value) => {
        group.scope = value;
        onEdit();
      },
    });
  }

  //: The visit column can never be the patient column — the spec refuses that
  //: outright, because keying both on one column collapses every visit — so the
  //: choice is not offered rather than refused after the fact.
  function onPatientKey(value) {
    group.patient = value;
    if (group.encounter === value) group.encounter = "";
    Shell.fillChooser(visitTrigger, visitEntries());
    visitTrigger.value = group.encounter;
    renderTable();
    onEdit();
  }

  // ─── The table ────────────────────────────────────────────────

  function headRow() {
    const head = document.createElement("div");
    head.className = "mapping-row mapping-head";
    head.setAttribute("role", "row");
    for (const label of ["Column in your file", "Goes to", "How it is read", "Confidence"]) {
      const heading = document.createElement("span");
      heading.setAttribute("role", "columnheader");
      heading.textContent = label;
      head.appendChild(heading);
    }
    return head;
  }

  function targetEntries() {
    return [KEEP_UNMAPPED].concat(
      targetChoices.map((path) => ({ value: path, label: targetLabel(path), note: path }))
    );
  }

  //: What the argument choosers say for the verb currently picked, rebuilt
  //: whenever the verb changes. Each handler reads the CURRENT spec rather than
  //: the one it was built against, so changing the separator cannot strand the
  //: piece that was chosen beside it.
  function paintArgument(host, column) {
    host.textContent = "";
    const spec = picks.get(column).transform;
    const verb = verbOf(spec);
    if (verb === "parse_date:" || verb === "parse_datetime:") {
      const entries = verb === "parse_date:" ? DATE_PATTERNS : DATETIME_PATTERNS;
      addLabelledChooser(host, {
        label: "Written like",
        entries: entries,
        value: argOf(spec),
        pick: "pattern",
        onChange: (value) => setTransform(column, verb + value),
      });
    } else if (verb === "split:") {
      const parts = splitArgs(argOf(spec));
      addLabelledChooser(host, {
        label: "Separated by",
        entries: DELIMITERS,
        value: parts[0],
        pick: "delimiter",
        onChange: (value) =>
          setTransform(column, `split:${value}:${splitArgs(argOf(picks.get(column).transform))[1]}`),
      });
      addLabelledChooser(host, {
        label: "Which piece",
        entries: POSITIONS,
        value: parts[1],
        pick: "position",
        onChange: (value) =>
          setTransform(column, `split:${splitArgs(argOf(picks.get(column).transform))[0]}:${value}`),
      });
    } else if (verb === "const:") {
      addLiteral(host, column, argOf(spec));
    }
  }

  //: The one free-text control on the surface, and it is safe to be one: the
  //: operator is authoring their own wording, so it structurally cannot echo a
  //: cell from the file.
  function addLiteral(host, column, value) {
    const id = nextId();
    const input = document.createElement("input");
    input.type = "text";
    input.id = id;
    input.dataset.pick = "literal";
    input.value = value;
    input.addEventListener("input", () => setTransform(column, `const:${input.value}`));
    host.appendChild(Shell.makeField(id, "The wording to use", "", input));
  }

  function setTransform(column, spec) {
    picks.get(column).transform = spec;
    onEdit();
  }

  function buildRow(suggestion) {
    const column = suggestion.source;
    const row = document.createElement("div");
    row.className = "mapping-row";
    row.setAttribute("role", "row");
    // The hook the acceptance test finds a row by, and the anchor a refusal
    // scrolls to.
    row.dataset.source = column;

    const nameCell = cell(row);
    nameCell.appendChild(document.createTextNode(column));
    const evidence = evidenceOf(suggestion);
    if (evidence) {
      const line = document.createElement("div");
      line.className = "mapping-evidence";
      line.textContent = evidence;
      nameCell.appendChild(line);
    }

    const targetCell = cell(row, "mapping-cell");
    const readCell = cell(row, "mapping-cell");
    const role = roleOf(column, group);
    if (role) {
      targetCell.textContent = `Used as: ${ROLE_LABEL[role]}`;
    } else {
      addCellChooser(targetCell, {
        spoken: `Where ${column} goes`,
        entries: targetEntries(),
        value: picks.get(column).target,
        pick: "target",
        onChange: (value) => {
          picks.get(column).target = value;
          onEdit();
        },
      });
      const args = document.createElement("div");
      args.className = "mapping-cell";
      addCellChooser(readCell, {
        spoken: `How ${column} is read`,
        entries: TRANSFORMS,
        value: verbOf(picks.get(column).transform),
        pick: "transform",
        onChange: (value) => {
          picks.get(column).transform = defaultSpec(value);
          paintArgument(args, column);
          onEdit();
        },
      });
      readCell.appendChild(args);
      paintArgument(args, column);
    }

    rows.set(column, { node: row, score: cell(row), suggestion: suggestion });
    return row;
  }

  function renderTable() {
    const table = el("format-mapping");
    table.textContent = "";
    rows.clear();
    table.appendChild(headRow());
    for (const suggestion of proposal.suggestions || []) table.appendChild(buildRow(suggestion));
    paintScores();
  }

  //: A confidence belongs to the machine's own pick. Once a person has decided
  //: otherwise the number describes a match nobody made, so the row says
  //: "Edited" instead — plain text, no tint: the ladder is for clinical status.
  function paintScores() {
    const was = reviewOf(proposedGroup, proposedPicks);
    const now = reviewOf(group, picks);
    for (const [column, entry] of rows) {
      if (roleOf(column, group)) {
        entry.score.textContent = "";
      } else if (
        roleOf(column, proposedGroup) ||
        !samePair(was.decisions[column], now.decisions[column])
      ) {
        entry.score.textContent = "Edited";
      } else {
        entry.score.textContent = `${Math.round((entry.suggestion.confidence || 0) * 100)}%`;
      }
    }
  }

  // ─── Rendering an answer ──────────────────────────────────────

  function renderProposal(res) {
    proposal = res;
    const suggestions = res.suggestions || [];
    columns = suggestions.map((s) => s.source);
    // The closed chooser set, plus any target the proposal already names: a
    // destination on screen must always be one the chooser can show.
    targetChoices = (res.targets || []).slice();
    for (const s of suggestions) {
      if (s.target && !targetChoices.includes(s.target)) targetChoices.push(s.target);
    }
    targetChoices.sort();

    proposedGroup = {
      patient: keyValue(res.patient_key),
      encounter: keyValue(res.encounter_key),
      scope: String(res.row_scope || "patient"),
    };
    group = Object.assign({}, proposedGroup);
    proposedPicks = new Map(
      suggestions.map((s) => [s.source, { target: s.target || "", transform: s.transform || "strip" }])
    );
    picks = new Map();
    for (const [column, pick] of proposedPicks) picks.set(column, Object.assign({}, pick));

    // What the file is. How it is grouped is no longer prose: it is the three
    // controls below, which stay true when the operator changes one.
    el("format-grouping").textContent =
      `${String(res.format).toUpperCase()} file · ${res.columns} columns.`;

    renderStructure();
    renderTable();
    renderChanges();
    el("format-summary").textContent = (res.summary || []).join("\n");
    el("format-proposal").hidden = false;
    // Consent is per-analysis, never sticky.
    el("format-confirm").checked = false;
    el("format-save").disabled = true;
    setStep(REVIEW_STEP);
  }

  function renderSaved(res) {
    el("format-result-path").textContent = `The format was saved to ${res.mapping_dir}`;
    el("format-result-md").textContent = res.mapping_md || "";
    el("format-result").hidden = false;
    setStep("Done. This export format is now available when you rebuild charts.");
  }

  // ─── Anchoring a refusal ──────────────────────────────────────

  function clearAttention() {
    const panel = el("format-proposal");
    for (const node of panel.querySelectorAll(".row-attention")) {
      node.classList.remove("row-attention");
    }
    for (const note of panel.querySelectorAll(".row-note")) note.remove();
  }

  function noteOn(host, text) {
    const note = document.createElement("div");
    note.className = "row-note";
    note.textContent = text;
    host.appendChild(note);
  }

  function markRow(column, text) {
    const found = rows.get(column);
    if (!found) return false;
    found.node.classList.add("row-attention");
    if (text) {
      const note = cell(found.node, "row-note");
      note.textContent = text;
    }
    return true;
  }

  //: The whole point of the structured pointer: the refusal stops being a
  //: sentence to read and becomes a place to look. Every noun in it is a column
  //: name, a target label, a transform label or a mask — never a cell.
  function loadFailureSentence(res) {
    const found = rows.get(res.detail_column);
    const said = ["This column could not be read the way it is set."];
    const shape = found ? shapeSentence(found.suggestion) : "";
    if (shape) said.push(`${res.detail_column} looks like ${shape}.`);
    const set = [];
    if (res.detail_transform) set.push(lowerFirst(transformLabel(res.detail_transform)));
    if (res.detail_target) set.push(`going to ${targetLabel(res.detail_target)}`);
    if (set.length) said.push(`It is set to ${set.join(", ")}.`);
    said.push("Pick a different way to read it, or send it to a different field.");
    return said.join(" ");
  }

  function anchorLoadFailure(res) {
    const marked = res.detail_column ? markRow(res.detail_column, loadFailureSentence(res)) : false;
    if (marked) rows.get(res.detail_column).node.scrollIntoView({ block: "center" });
    Shell.showBanner(
      marked
        ? "This file cannot be read with the match-up below yet — the marked row says which column, and why."
        : "This file cannot be read with the proposed match-up yet. Change how one of the columns is read, then look at the example again."
    );
  }

  //: Not a per-column fault at all: a column loses values because the row grain
  //: collapsed it, so the refusal points at the grouping controls.
  function anchorDroppedColumns(res) {
    const host = el("format-structure");
    host.classList.add("row-attention");
    noteOn(
      host,
      "Grouped this way, those columns lose what is in them. Change which column " +
        "identifies a visit, or what one row of the file is."
    );
    host.scrollIntoView({ block: "center" });
    Shell.showBanner(
      `Cannot save yet — these columns would be lost: ${(res.dropped || []).join(", ")}. ` +
        "Every column must have a home before the format is saved."
    );
  }

  //: Two columns aimed at one field is the collision this makes reachable, and
  //: the detail names both — so both rows are marked, and the operator is
  //: looking at the pair rather than hunting for it.
  function anchorCannotBuild(res) {
    const detail = String(res.detail || "");
    const named = columns.filter((column) => detail.includes(column));
    for (const column of named) markRow(column, "");
    Shell.showBanner(
      `This match-up cannot be built yet: ${detail}` +
        (named.length ? " The marked rows are the ones it names." : "")
    );
  }

  const ANCHORED = {
    WouldDropColumns: anchorDroppedColumns,
    MappingLoadFailed: anchorLoadFailure,
    CannotBuildMapping: anchorCannotBuild,
  };

  //: Own keys only. An error code arriving as "constructor" would otherwise
  //: find a function on Object.prototype and be called as an anchor.
  function anchorFor(error) {
    return Object.prototype.hasOwnProperty.call(ANCHORED, error) ? ANCHORED[error] : null;
  }

  // ConfirmationRequired is the EXPECTED outcome of step 1. The three refusals
  // keep their loud semantics and now carry the proposal with them, so each one
  // points AT something instead of describing it.
  function route(res) {
    const anchor = res ? anchorFor(res.error) : null;
    if (res && res.ok) {
      renderSaved(res);
    } else if (res && res.error === "ConfirmationRequired") {
      renderProposal(res);
    } else if (anchor) {
      // A refusal reached before any proposal was painted still carries one.
      if (!proposal && (res.suggestions || []).length) renderProposal(res);
      clearAttention();
      anchor(res);
      setStep(REVIEW_STEP);
    } else {
      Shell.showBanner(
        `The example could not be turned into a format: ${res ? res.error : "no answer from the app"}`
      );
    }
  }

  async function fetchResult() {
    if (!hasApi()) return;
    try {
      route(await window.pywebview.api.last_source_result());
    } catch (err) {
      setAnalyzing(false);
      Shell.showBanner(String(err));
    }
  }

  // Both the terminal stage AND an error fetch the stashed result: the result
  // carries the outcome-specific detail (which columns, which transform) that a
  // bare error string does not.
  //: Held from the click until the run's terminal event.
  //:
  //: NOT `Shell.guardButton`: that releases when its `work` resolves, and
  //: `source_init_async` resolves as soon as the WORKER STARTS. Three rapid
  //: clicks still fired three analyses through it — measured, not assumed.
  //: The only on-screen feedback here is the step line, which is not a live
  //: region, so a screen-reader operator gets nothing from a click and will
  //: reasonably click again.
  let analyzeLabel = "";
  function setAnalyzing(busy) {
    const button = el("format-analyze");
    if (!button) return;
    // Remember the button's OWN label rather than re-typing it here, so the
    // markup stays the single place the wording lives.
    if (!analyzeLabel) analyzeLabel = button.textContent;
    button.disabled = busy;
    button.textContent = busy ? "Looking…" : analyzeLabel;
  }

  function onEvent(event) {
    if (event.type === "done" || event.type === "error" || event.state === "done") {
      setAnalyzing(false);
    }
    if (event.type === "stage" && event.stage === "source" && event.state === "done") {
      fetchResult();
    } else if (event.type === "done" || event.type === "error") {
      fetchResult();
    }
  }

  function values() {
    return {
      example: el("format-example").value,
      name: el("format-name").value,
      display: el("format-display").value || null,
    };
  }

  async function onAnalyze() {
    if (!hasApi()) return;
    Shell.hideBanner();
    el("format-result").hidden = true;
    const v = values();
    if (
      !Shell.requireFields([
        [v.example, "the example export to learn from", "format-example"],
        [v.name, "a short name for this format", "format-name"],
      ])
    ) {
      return;
    }
    setStep("Step 1 of 2 — looking at the example…");
    setAnalyzing(true);
    try {
      // Looking again re-runs the scorer over the same file, so it carries no
      // review: there is nothing yet for one to correct.
      const started = await window.pywebview.api.source_init_async(
        v.example,
        v.name,
        v.display,
        false,
        null,
        null
      );
      if (started && started.ok === false) {
        Shell.showBanner(Shell.refusalText(started.error, "The example could not be read"));
        setStep("Step 1 of 2 — look at the example.");
        setAnalyzing(false);
      }
    } catch (err) {
      Shell.showBanner(String(err));
    }
  }

  //: An empty wording is a transform the loader cannot parse. Caught here, in
  //: the app's own missing-field language, rather than as a refusal after a
  //: round trip.
  function literalsFilled() {
    const blank = Array.from(document.querySelectorAll('#format-mapping [data-pick="literal"]')).find(
      (input) => !input.value.trim()
    );
    return blank ? Shell.requireFields([["", "the wording to use", blank.id]]) : true;
  }

  async function onSave() {
    if (!hasApi() || !el("format-confirm").checked) return;
    if (!literalsFilled()) return;
    Shell.hideBanner();
    clearAttention();
    const v = values();
    setStep("Step 2 of 2 — checking that no column would be lost…");
    try {
      const started = await window.pywebview.api.source_init_async(
        v.example,
        v.name,
        v.display,
        true,
        null,
        currentReview()
      );
      if (started && started.ok === false) {
        Shell.showBanner(Shell.refusalText(started.error, "The format could not be saved"));
        setStep(REVIEW_STEP);
      }
    } catch (err) {
      Shell.showBanner(String(err));
    }
  }

  function init() {
    el("format-analyze").addEventListener("click", onAnalyze);
    el("format-save").addEventListener("click", onSave);
    el("format-confirm").addEventListener("change", () => {
      el("format-save").disabled = !el("format-confirm").checked;
    });
    Shell.onReady((live) => {
      el("format-analyze").disabled = !live;
    });
  }

  Shell.registerFlow("source_init", onEvent);
  init();
})();
