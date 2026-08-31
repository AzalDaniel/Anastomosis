# Anastomosis design language — “Porcelain & Oxblood”

The one visual and verbal system for every surface the product shows: the
desktop GUI, the CLI/TUI, the Windows installer, and the offline archive.
Derived from three sources, in priority order: the vessel mark
(`assets/icon/icon.svg` — oxblood on porcelain), the maintainer’s Tebra
console (the reference implementation this system generalises), and Apple’s
Liquid Glass material rules (WWDC25) adapted to a professional clinical tool
on Windows/WebView2.

Two registers, one philosophy: **calm instrument**. The product handles
patients’ charts; nothing on screen may be decorative noise, falsely
cheerful, or engineering shop-talk. Every element earns its place by helping
a clinician answer “what is happening to my charts, and what do I do next?”

---

## 1. Color

Ground and ink (dark, warm — never navy, never gray-blue):

| Token | Value | Role |
| --- | --- | --- |
| `--ground` | `oklch(0.16 0.013 50)` | window canvas (warm near-black) |
| `--ground-deep` | `oklch(0.125 0.012 50)` | the drawer and the deepest wells |
| `--ink` | `oklch(0.96 0.010 80)` | primary text (porcelain) |
| `--ink-secondary` | `oklch(0.84 0.012 80)` | supporting text; the floor inside a tinted row |
| `--ink-muted` | `oklch(0.72 0.012 80)` | captions — 12 px minimum, never smaller |
| `--ink-inverse` | `oklch(0.15 0.010 55)` | text on the porcelain lozenge |
| `--brand-bright` | `oklch(0.55 0.15 30)` | interactive brand (primary buttons, the ON switch, focus rings) |
| `--brand-press` | `oklch(0.49 0.15 30)` | hover and pressed — never a resting state |
| `--lozenge` | `rgba(246, 240, 231, 0.94)` | the nav pill's selected slot |

**Two of these are measured floors, not taste.** `--ink-muted` below 0.72 puts
every 12 px caption on a panel under 4.5 : 1 — five failures, counted on the
shipped surfaces. `--brand-bright` above 0.55 puts the porcelain label on the
primary button under it: at 0.56 it measured 4.46 : 1, and that was shipping.
Neither may be moved without re-measuring both.

**Selection is porcelain; action is oxblood.** That is the whole colour grammar
of the interactive layer, and it is why the underline on a selected tab is
`--ink` rather than the brand — oxblood at 2 px on a panel measures ≈2.3 : 1 and
would fail WCAG 1.4.11.

Clinical signals are their own family — never reused as decoration, never
replaced by brand color:

| Token | Value | Meaning |
| --- | --- | --- |
| `--ok` | `oklch(0.80 0.13 160)` | verified / completed |
| `--attention` | `oklch(0.84 0.12 85)` | in progress / needs review |
| `--stop` | `oklch(0.70 0.19 25)` | failed / refused — always paired with text |

Banned outright (the AI-slop catalog, `scratchpad` research, applies in
full): indigo/purple→teal gradients, gradient text, aurora/mesh backdrops,
neon glow, any gradient used as a color scheme rather than as glass light.

## 2. Glass, and what it is allowed to touch

> Apple's rule, verbatim: glass belongs to the **functional layer** — the
> controls floating above content — and never to the content layer itself.

This document used to open with a backdrop section: a flat ground carrying an
oversized blurred vessel mark, "the thing glass refracts". That premise was
false. Panels were glass too, so the mark refracted through a translucent panel
onto a translucent nav, and the composite measured **1.188 : 1** against the
ground — a panel you could not see the edge of. The mark is deleted and the
premise with it.

**Glass is one tier, plus one sheet.**

| Tier | Where | backdrop-filter | background | border |
| --- | --- | --- | --- | --- |
| glass | the nav pill, the About circle, the activity strip | `saturate(150%) blur(24px)` | `rgba(244 238 228 / 0.10)` | `1px solid rgba(244 238 228 / 0.36)` |
| sheet | the About popover, the chooser popup, the drawer | `saturate(170%) blur(48px)` | `rgba(24 20 16 / 0.94)` | `1px solid rgba(244 238 228 / 0.24)` |

* The sheet tier is near-opaque **on purpose** — two stacked backdrop-filters
  turn to mud, so anything floating above something that already has one gets
  94 % ground plus its own `isolation: isolate` (the Tebra mud fix; the Denim
  rule). Legibility outranks prettiness.
* Glass catches light through an inset top highlight, not an outer glow.
* Shadow belongs to true floaters only. In-flow panels get the hairline.
* The border alpha is 0.36 because that is the first value whose boundary
  clears 3 : 1 over **both** the ground and a bright panel scrolled beneath it.
  0.32 left the strip at 2.81 : 1.

Refusing transparency is four token overrides — `--glass-bg`, `--glass-blur`,
`--glass-modal-bg`, `--glass-modal-blur` — because every declaration in the app
reads one of them. Two triggers: `prefers-reduced-transparency`, and an in-app
switch, which exists because WebView2 does not report that preference on every
Windows build. In the fallback the border alpha goes **up**, to 0.52: without
the blur the edge is the only thing holding the pill off the background.

## 3. Content surfaces — opaque, and a ladder

Content is never glass. These are flat fills, and the two floors below are
testable:

| Token | Value | Role |
| --- | --- | --- |
| `--surface` | `oklch(0.35 0.013 55)` | panels |
| `--field` | `oklch(0.265 0.012 52)` | inputs, a recessed well |
| `--field-focus` | `oklch(0.235 0.012 52)` | a focused input |
| `--surface-raised` | `oklch(0.255 0.013 50)` | the chrome tier's opaque twin, for the transparency fallback |

* **A panel must clear 1.6 : 1 against the ground.** It measures 1.713 : 1. At
  the old translucent value it was 1.188 : 1.
* **A field must clear 1.3 : 1 against its panel.** It measures 1.351 : 1.

## 4. Type

* **Chrome and data: Mona Sans VF** (bundled) — pushed off its defaults so
  it stays ours: view titles at `wdth 115 / wght 700`, body at
  `wdth 100 / wght 440`, dense table labels at `wdth 85 / wght 500`.
* **Values, ids, timestamps: JetBrains Mono VF** with
  `font-variant-numeric: tabular-nums` everywhere a number can change.
* **Patient names and record titles: Fraunces VF** (OFL) if bundled — the
  editorial register; otherwise Mona Sans wide/heavy carries it.
* Scale: 12 / 13 / 15 / 18 / 24 / 34 px. Nothing below 12 px, ever — the
  smallest text in a chart tool carries audit-relevant facts.
* Hierarchy comes from weight and size only. No gradient text, no italics as
  decoration.
* **The uppercase carve-out, bounded.** Letter-spaced all-caps appears in
  exactly one place: the label under a value display. 12 px floor, weight 600,
  tracking 0.10em, `--ink-muted`, at most three words, never a sentence. The
  corollary is that **table column headers are sentence case**, so the eye has
  one uppercase idiom to learn rather than two it must tell apart. (Both were
  uppercase in the first prototype and the heading could not be distinguished
  from the label 24 px away.)
* **The face follows the kind of content, never the tag.** Monospace is for
  strings a person reads character by character and could mistype: paths,
  identifiers, host:port, visit ids, hashes, timestamps, and any number that
  can change. Everything a person composes in their own words is the body face.
  Implementation is a body-face default plus two opt-in classes, so a new field
  is safe by omission.

## 5. Shape and space

* Radius by role, never one token: panels 14 px, controls 8 px, chips 6 px.
  The pill (999) belongs to one control — the view nav — because a sliding
  pill means "peer destinations" and the app has one such choice. A binary
  setting is a switch; one of N is a chooser. (The switch track, the progress
  bar and the activity strip are round-ended shapes, not pills.) Everything
  else that used to be a capsule is a bordered control or plain text.
* Spacing on the 4-px scale, and each step means something:

  | Step | Applies to |
  | --- | --- |
  | 4 px | inside one control — label to input, value to its label, icon to text |
  | 8 px | between siblings in one group |
  | 12 px | between a control and its help line |
  | 16 px | between groups inside a panel |
  | 24 px | between panels, and the panel's own padding |
  | 32 px | between a view's regions |

  Whitespace encodes structure; a gap that means nothing is a gap that should
  not be there.
* **Every control clears 44 px on its short axis.** Not 24 px — WCAG 2.5.8's
  AA floor is the minimum below which a target is a defect, not a target to aim
  at. This was 52 of 69 controls under 44, twelve of them under 24.
* **One accent per row, and the tint is it.** The 3 px left border and the
  status badge are both retired; see §5b.

## 5b. Status tints — a ladder, not three hues

Four row states, on the content layer only:

| Token | Mix | vs ground |
| --- | --- | ---: |
| *(waiting)* | no tint — the bare ground | 1.000 : 1 |
| `--tint-filed` | `--ok` 10 % into `--ground` | 1.134 : 1 |
| `--tint-progress` | `--attention` 16 % into `--ground` | 1.280 : 1 |
| `--tint-attention` | `--stop` 32 % into `--ground` | 1.561 : 1 |

**These are a luminance ladder ordered by urgency, not three hues of equal
weight, and the difference is load-bearing.** The equal-weight version was
built and simulated: amber and red become the *identical* colour under
deuteranopia, and every pair falls below the just-noticeable step under
protanopia. The ladder holds monotone at every step under protanopia,
deuteranopia and achromatopsia — worst step ×1.64 against a ×1.55 floor.

The loudest state is the lightest and most saturated thing on screen; success
is the quietest. Any edit to these values must re-run the simulation.

Four rules come with the tint:

1. **The plain-English state text is the primary carrier**; the tint is
   redundant reinforcement (WCAG 1.4.1). A monotone ladder is still a UI that
   stops working in greyscale if colour is the only signal.
2. **A tint earns its place only when rows differ.** A list where every row has
   the same status gets hairlines and no tint — an unvarying tint carries no
   information.
3. **One accent per row**, and the tint is it.
4. `--ink-muted` never appears inside a tinted row; `--ink-secondary` is the
   floor there, and measures 7.63 : 1 on the loudest tint.

## 6. Motion

Four durations, one curve — `cubic-bezier(0.32, 0.72, 0, 1)`:

| Token | ms | Used for |
| --- | --- | --- |
| `--t-snap` | 120 | hover, press |
| `--t-move` | 240 | view crossfade, popovers, rows |
| `--t-soft` | 480 | log fade-in, halo bloom |
| `--t-fill` | 800 | progress bars |

Hover feedback is colour/opacity only. Scale "squish" is reserved for the nav
pill and sheet presentation. Progress animates only while a run is in flight.

`prefers-reduced-motion` zeroes every duration **and delay**, and forces
`scroll-behavior: auto`. Three fades survive, because the HIG's rule is to
*replace* travel with a fade rather than delete it: the nav lozenge, the view
crossfade, and the drawer. A lozenge that teleports between two view names is
harder to follow than one that moves, and a view that swaps with no crossfade
reads as a page load — the one thing the single-document shell exists to
avoid.

## 7. The shell (why nothing flashes)

The GUI is ONE document. The five legacy pages become views inside a single
`index.html`; the nav switches views by toggling `hidden` + a 240 ms
crossfade. Consequences, all load-bearing:

* CSS, fonts, the gooey SVG defs, and the backdrop parse once per app
  launch — no FOIT, no repaint, no filter re-registration per click.
* The pywebview bridge attaches once; no per-navigation re-race.
* Event streams survive navigation: a run started in one view keeps its
  progress visible from every view via the shared activity strip. “— idle —”
  while a run is in flight is a lying UI and is gone.
* First paint is the skeleton (nav + panels), immediately; data hydrates in.

## 8. Iconography

One inline SVG set: 20×20 viewBox, `stroke: currentColor`,
`stroke-width: 1.5`, no fills. The `✓ ⚠ ✗ ·` glyph constants are replaced by
this set. No emoji anywhere in the UI. No sparkles anywhere, ever.

## 9. Information architecture — four views

| View | Was | Job |
| --- | --- | --- |
| **Charts** | Dashboard | “Turn an EHR export into charts I can keep”: pick the export folder, where results go, run, watch four plain-English stages, see per-patient results |
| **Migrate** | Migration wizard | “Move charts into another system”: detect → choose destination → see the routes in plain language → run — and hand off to Uploads WITH context (destination and pack pre-filled, never re-typed) |
| **Uploads** | Upload console | “Watch and drive charts being filed”: progress ledger with plain-English states, start/stop, review calendar |
| **Teach** | Pack from samples + Learn a source | One workspace, two modes: “teach it your document layout” / “teach it your export format” — the two legacy wizards were function-for-function mirrors |

The duplicated run-form between Dashboard and Wizard collapses: Charts owns
the plain reconstruction run; Migrate owns the destination run; both share
one form component rather than two re-implementations.

## 9b. The content layer

Three components carry every result the app has to show, and none of them is
glass.

**Value displays** replace boxed counters. A mono numeral over its name in
small caps, no container at all — a count is already the smallest thing that
can be said, and boxing it and explaining it underneath is how four numbers
grow to fill half a window. Colour is earned by a number that asks for
something; **success is never coloured, because success is the absence of a
coloured number**, and a zero is never coloured whatever bucket it belongs to.

**Result rows** are the main surface, and they sit on the ground rather than
inside a panel — the form above is opaque content, the list below is rows on
the canvas, and the two planes reading differently is the point. 44 px when the
row is a target, 36 px when it is data. Tinted per §5b.

**Empty states** are two clauses: what is not here, then the one thing that
fills it. No icon, no illustration, no button, **and no zeros** — four zeros on
a screen that has never run is the same lie as "— idle —" during a live run.
The region keeps its heading either way; what swaps is the list and the
sentence.

## 10. Copy — the register rules

Audience: a physician or practice manager. Competent, busy, not an
engineer. Rules, enforced against the audit’s string inventory:

1. No CLI commands, flags, file paths, or module paths in GUI copy. The GUI
   is the product, not a brochure for the terminal.
2. No engineering vocabulary: ~~pipeline, manifest, ledger, CDP endpoint,
   selectors, item-key, histogram, round-trip, ritual, milestone, payload,
   operator surface, identifier, extra~~. Each has a plain replacement in
   the copy map (`docs/design/COPY_MAP.md`).
3. Internal mechanism names are not user-facing nouns: “PHI-safe summary”
   → “No patient data is shown below.”
4. States render as plain English with the technical id in a tooltip:
   `pre_verify_failed` → “Stopped before filing — identity check did not
   pass.”
5. The word “terminal” never labels a bucket that includes failures.
   Buckets: **Filed** (green), **Needs attention** (red), **In progress**
   (amber), **Waiting** (neutral).
6. Factual, calm, no exclamation marks, no emoji, no cheerleading. “No
   pending results.” — not “You’re all caught up! 🎉”
7. The app name appears exactly once (the OS title bar). The version lives
   in an About popover.
8. Advanced inputs (debug port, skiplists) live behind one consistent
   “Advanced” disclosure per form, off by default, each with a one-line
   plain-English explanation of when a person would need it.
9. One technical register everywhere: the same feature is never layman on
   one view and systems-engineer on another.
10. **A field earns a help line only if the label cannot carry the point** —
    the consequence is not recoverable from it, the value must come from
    outside the app and the reader needs telling where, there is a format the
    placeholder cannot show, or it is an Advanced field and needs one line
    saying when a person would want it. It does not earn one when the
    placeholder already says it, when it restates the label, when it only says
    the field is optional (that belongs in the label), or when it explains a
    *result* rather than an input. Uniform emphasis is no emphasis: two views
    once carried more help lines than fields, and most of them restated the
    label above them.
11. **A machine identifier is never a visible label.** `generic_soap` reads
    "Generic SOAP"; the identifier rides the chooser row's mono caption and the
    row's tooltip, so support can still ask which one it says. This is a
    control rule as much as a copy rule — the old `<select>` had one text slot
    per option, and the identifier took it.

## 11. Per-surface notes

* **CLI/TUI**: same register, same state vocabulary — but NOT the same
  palette. The GUI may use the tokens above because it draws its own ground;
  a terminal cannot, because the background belongs to the person running it.
  Approximating porcelain in truecolor put primary text at 1.13 : 1 on a
  light theme and a refusal at 2.90 : 1, and no absolute palette fixes that
  in both directions: the oxblood measured 8.30 : 1 on white and 2.53 : 1 on
  black. So the CLI names colors the terminal resolves — `default` for text,
  `dim` for the supporting register, `green`/`yellow`/`red` for the clinical
  signals, and weight rather than hue for identity, since `red` is the
  refusal color and identity must not borrow it. `anast` bare = a guided,
  numbered flow a clinician can follow; every prompt states what happens
  next. Rich tables use the same Filed/Needs-attention/In-progress/Waiting
  words as the GUI, and those words carry the meaning without the color.
  The guided session opens on the vessel mark itself, in dots: the logo's own
  geometry sampled to a character grid (generated by `tools/make_vessel.py`
  into `core/vesselmark_data.py`, so the terminal mark and the taskbar icon
  cannot drift apart), with the greeting beside it. Its gradient obeys the
  paragraph above — density and text weight climbing together, `dim` → plain →
  `bold`, never a hue — so it reads the same where a terminal renders neither.
  It assembles once, in under a second, from the trunk outward, and stands down
  entirely for three readers who are not watching it: a stream that is not a
  terminal, a window too narrow to hold the mark and a legible column of text,
  and anyone who set `NO_COLOR`. A console that cannot encode the round dots
  gets an ASCII ramp of the same shape, the same fallback the status glyphs
  already take.
* **Installer**: dark warm ground, the rounded porcelain tile as wizard
  imagery, modern wizard style, sentence-case copy in the same register;
  no marketing prose.
* **Offline archive**: stays light/porcelain (it is a document, printed or
  read anywhere) but inherits type scale, spacing, and copy register.

## 12. What was removed, and why (the anti-slop ledger)

* Gradient wallpaper + veil (two full-viewport decorative gradient layers),
  then the blurred brand watermark that replaced them → one flat ground and
  nothing else. Glass belongs to the chrome that floats over content, and what
  it refracts is the content; a decorative layer was standing in for a content
  layer that had not been designed.
* Dichroic shimmer progress gradient → flat `--brand-bright` fill; shimmer
  only as a subtle running-state pulse.
* Dead `.pill` filter-bar CSS (never instantiated) → deleted.
* 15+ pill-radius declarations → role radii (§5).
* App name ×4 + raw “—” version placeholder on every page → once + About.
* Command palette on 3 of 5 pages for ≤ 8 actions → deleted; its one real
  use (finding an upload item by id) becomes a visible search field in
  Uploads.
* `✓ ⚠ ✗ ·` glyph icons → the SVG set.
* Two unrelated stat-tile grids → one status component with the §10.5
  buckets and correct grid areas, then the tiles themselves → value displays
  with no container (§9b).
* The status badge and the `.chip` focus ring → deleted; both styled nothing
  that was ever rendered.
* A second sliding pill for a binary setting → a switch, which collapsed the
  toggle mechanism's two ARIA vocabularies into the one it has a caller for.
* Teach's mode chips → underline tabs. Choosing which content a panel shows
  belongs to the content layer; the chips were a second navigation idiom
  directly under the actual navigation.
* Every native `<select>` → the chooser (§9b). It was the one control the
  browser tests could not see, it could not be styled past a point, and its
  single text slot per option is why operators read `generic_soap`.
* Eight tokens nothing referenced — the three `--glass-veil-*` of the deleted
  tier, `--surface-row`, the three hover tints, and `--brand`. A token defined
  and unused is a decision that looks made and is not.
* 20 of 29 field help lines → deleted (§10, rule 10).
