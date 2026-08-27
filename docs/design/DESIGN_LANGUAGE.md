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
| `--ground` | `oklch(0.18 0.012 55)` | window canvas (warm near-black) |
| `--ground-deep` | `oklch(0.14 0.010 55)` | title bar, wells |
| `--ink` | `oklch(0.96 0.010 80)` | primary text (porcelain) |
| `--ink-secondary` | `oklch(0.84 0.012 80)` | supporting text |
| `--ink-muted` | `oklch(0.68 0.012 80)` | captions — 12 px minimum, never smaller |
| `--brand` | `oklch(0.44 0.13 30)` | the oxblood of the mark; identity moments only |
| `--brand-bright` | `oklch(0.56 0.15 30)` | interactive brand (links, active nav, primary buttons) |

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

## 2. The backdrop

One layer, not three: a flat `--ground` canvas carrying a single oversized,
blurred rendering of the vessel mark at ≤ 5 % opacity, anchored bottom-right
— the thing glass refracts. No animated gradients, no veil stacking. The
backdrop never changes per view and never repaints on navigation (see §7).

## 3. Glass

Three tiers, scaled together (blur + saturation + opacity + border move as
one — depth is correlated, per the Tebra reference):

| Tier | backdrop-filter | background | border |
| --- | --- | --- | --- |
| veil (nav, strips) | `blur(24px) saturate(130%)` | `rgba(244 238 228 / 0.05)` | `1px solid rgba(244 238 228 / 0.10)` |
| card (panels) | `blur(32px) saturate(150%)` | `rgba(244 238 228 / 0.08)` | `1px solid rgba(244 238 228 / 0.18)` |
| modal (popover, sheet) | `blur(48px) saturate(170%)` | `rgba(24 20 16 / 0.94)` | `1px solid rgba(244 238 228 / 0.24)` |

Rules:
* The modal tier is near-opaque **on purpose** — two stacked
  backdrop-filters turn to mud, so anything floating above a card gets the
  94 % ground + its own `isolation: isolate` (the Tebra mud fix). This is
  also the Denim rule: overlays are MORE opaque than ambient glass, because
  legibility outranks prettiness.
* Glass catches light through an inset top highlight
  (`inset 0 1px 0 rgba(255 250 240 / 0.12)`), not through outer glow.
* Shadow only on true floaters (modal/popover/toast):
  `0 24px 64px rgba(0 0 0 / 0.35)`. In-flow panels get the hairline border,
  never both.

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
* Hierarchy comes from weight and size only. No gradient text, no italics
  as decoration, no letter-spaced all-caps except the 10 px
  `AI-ASSISTED`-style provenance labels.

## 5. Shape and space

* Radius by role, never one token: panels 14 px, controls 8 px, chips 6 px.
  The pill (999) exists in exactly two places: the gooey segment toggle and
  a status badge. Everything else that used to be a capsule becomes a
  bordered control or plain text.
* Spacing on the 4-px scale: 4/8 inside a group, 24+ between groups —
  whitespace itself encodes structure (the Craft rhythm).
* One accent signal per row: status is a 3 px left border or one badge,
  never badge soup.

## 6. Motion

Four durations, one curve — `cubic-bezier(0.32, 0.72, 0, 1)`:

| Token | ms | Used for |
| --- | --- | --- |
| `--t-snap` | 120 | hover, press |
| `--t-move` | 240 | view crossfade, popovers, rows |
| `--t-soft` | 480 | log fade-in, halo bloom |
| `--t-fill` | 800 | progress bars |

Hover feedback is color/opacity only. Scale “squish” is reserved for the
segment toggle and sheet presentation. Every animation has a
`prefers-reduced-motion` override that zeroes it. Progress shimmer animates
only while running.

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

## 11. Per-surface notes

* **CLI/TUI**: same palette (oxblood/porcelain ANSI approximations), same
  register, same state vocabulary. `anast` bare = a guided, numbered flow a
  clinician can follow; every prompt states what happens next. Rich tables
  use the same Filed/Needs-attention/In-progress/Waiting words as the GUI.
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
  buckets and correct grid areas.
