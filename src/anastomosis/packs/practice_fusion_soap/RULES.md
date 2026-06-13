# practice_fusion_soap — PF forensic rules

The PF-specific rendering rules this pack carries, distilled from the
predecessor's gold standard (forensic analysis of 43 originals + 4 iteration
sprints, visually verified at >=2x DPI). Each rule states the behavior and the
reason it exists. This is a CARRY: the predecessor's template/CSS/section logic
is the default truth; divergences are marked **DIVERGENCE** with a reason.

**Identity rule (absolute).** The real clinic's identity is NEVER reproduced
here. Every redacted value below is synthesized from the fixtures or a neutral
placeholder; the deny-list scan enforces it.

---

## Page geometry (NEVER change)

- Letter, 612x792pt. Margins top `0.6in` / right `0.38in` / bottom `0.44in` /
  left `0.39in` (`pack.yaml.page` + the `@page` rule in `template.html`).
- Body font `Arial, Helvetica, sans-serif`, 9pt, line-height 1.45.

## print-color-adjust (CRITICAL — the 2-sprint bug)

`* { -webkit-print-color-adjust: exact !important; print-color-adjust: exact
!important; }`. Without it Chromium silently strips every `background-color` on
PDF export and the grey section bands render WHITE. The engine also sets
`print_background=True`; the CSS is the belt-and-suspenders. Honored verbatim.

## Forensic design tokens (measured from PF `get_drawings` fills)

| Token | Value | Where |
|---|---|---|
| Grey fill | `#f1f1f1` (rgb 241,241,241) — NOT #f2f2f2 | section/sub/th bands |
| Border | `#aaaaaa` (rgb 170,170,170) 0.8pt | section borders, dividers |
| Row-sep | `#e6e6e6` (rgb 230,230,230) 0.4pt | data-table `<td>` bottoms |
| Muted text | `#737373` (rgb 115,115,115) | header labels |
| Strong text | `#333333` (rgb 51,51,51) | |

All parameterized off `pack.yaml.tokens` (forensic defaults baked into the
template `:root`). The QA visual-token check (issue #4 QA) asserts `#f1f1f1` is
a painted fill; the e2e `test_forensic_heading_band_fill_is_painted` proves it
via PyMuPDF `get_drawings()`.

## Heading taxonomy (GOLD §4)

- **MAIN HEADINGS** — bold 9.5pt, grey fill, top+bottom 0.8pt borders
  (`.section-header`): every top-level heading EXCEPT "Vitals for this encounter".
- **SPECIAL MAIN HEADING** — bold 9.5pt, NO fill, top+bottom borders
  (`.section-header-nofill`): "Vitals for this encounter" ONLY.
- **SUB-HEADINGS** — REGULAR weight 8pt, grey fill, borders (`.sub-header`):
  PRIMARY/SECONDARY PAYER, CONTACT/FAMILY INFORMATION, PATIENT NOTES,
  LAB/IMAGING ORDERS, FAMILY HEALTH HISTORY (FREE TEXT), Active/Inactive/Historical.
- **SOCIAL-HISTORY sub-labels are the BOLD exception** (GOLD §6): the 17 labels
  AND the right-column "RECORDED" label are bold (`.sh-table .sh-label,
  .sh-recorded { font-weight: bold }`). Do NOT generalize this to other sub-headers.

### Border-collapse rule ("3 lines not 4", sprint-2 regression)

When a sub-header immediately follows a section-header, their borders overlap.
`.section-header + .sub-header { border-top: none; }` collapses them to one
shared line (section-header top+bottom = 2 lines + sub-header bottom = 1 line =
3 total). Do **NOT** use `margin-top: -0.8pt` — it hid the section-header bottom
border under the sub-header fill (zero visible lines, sprint-2 regression).

## Page-break rules (derived from the 43-PDF audit, GOLD §3)

- Hard `page-break-before: always` ONLY on: Active insurance, Diagnoses, Social
  history, Family health history. **Orders is deliberately NOT broken** —
  removing its forced break improved page-match 10/39 → 18/39 (sprint-3).
- `page-break-inside: avoid` (`.keep-together`) on: Advance Directive,
  Implantable devices, Screenings, Observations, Quality of care, Care plan,
  Addenda, PMH subsections. Immunizations intentionally NOT kept-together (flows
  with Current Medications in ~54% of originals).
- `orphans: 2; widows: 2` on prose bodies (tuned at 2, not 3+, or Chromium
  shoves whole paragraphs to the next page).
- `break-after: avoid-page` on section headers (never orphan a header alone).

## 35-section order (GOLD §4)

Demographics → Active ins → Inactive ins → Payment → Vitals (no-fill) → Vitals
flowsheet → Diagnoses → Drug/Food/Env Allergies → Current Meds (as of) →
Historical (inside meds) → Immunizations → Social history (17 subs) → Past
medical history → Family health history → FHH (free text) → Advance Directive →
Implantable devices → Active/Inactive health concerns → Active/Inactive Goals →
Subjective → Objective → Assessment → Plan → Orders → LAB/IMAGING ORDERS →
Screenings → Observations → Quality of care → Care plan → Addenda (conditional).

Every static section renders even when empty (the empty-state strings, GOLD §4,
are inventoried in `pack.yaml`/the template). Addenda is the one CONDITIONAL
section — heading only when addendum rows exist (GOLD §10).

## Medications + escript line (GOLD §5)

- Column header row (grey, 4-col): Active|Historical 35% / SIG 27% / START/STOP
  18% / ASSOCIATED DX 20%.
- Per-drug block: drug row, then for each prescription a hyphen bar
  (`.meds-hyphen` 6pt x 1.1pt black, ABOVE the line) + a full-width escript line:
  `{PREFIX} ({STATUS}): {date}  PRESCRIBER: …  SIG: …  REFILLS: …  QUANTITY: …`,
  then `.meds-drug-sep`.
- Drug display name `Generic (Brand) Strength Route DoseForm`; omit parens when
  generic == trade; brand-only fallback (`_med_display_name`).
- START/STOP: both → `MM/DD/YY - MM/DD/YY`; only-stop → `- MM/DD/YY` (historical);
  only-start → `MM/DD/YY`; neither → `-`.
- Active vs historical: Active when StopDate empty.
- "as of" date is the RENDER DAY (`date.today()`), NOT the encounter date.
- Med reconciliation answer hard-coded `No selection made` (no EHI signal).
- ESCRIPT/SCRIPT prefix + status come from the adapter's transaction-priority
  resolution (`sources/pf_tebra/escript.py`, the 20-description label map +
  granular priority; cancellation > verified, dispensing > cancellation,
  refills/changes do NOT override verified). The escript DATE is the "Order
  sent" transaction time in Eastern for ESCRIPT, prescription DoS for SCRIPT —
  rendered `MM/DD/YY`.

## Insurance (GOLD §7)

- Sort by `OrderOfBenefits` ASC (Primary 0 → Secondary 1 → … → Other 99).
- Active vs Inactive split on `InsurancePlanIsActive`; BOTH headings always render.
- Sub-header `{PRIORITY} PAYER - {COVERAGE}` (e.g. `PRIMARY PAYER - MEDICAL`).
- **TYPE column = the adapter-resolved `plan_type`** (the superbill `PlanType`
  three-tier join), shown `-` when unresolved — **NEVER** the generic
  `coverage_type` "Medical" (`_coverage_view`). The ported insurance QA fails if
  TYPE shows "Medical".
- Insurance + Payment share an identical 4-col 25/25/25/25 fixed grid.
- Copay: `-1`/empty → `-`; integers without decimals; else shortest repr.
- **Payment empty states** (gpdfs:950-961): every absent guarantor value
  renders `-`, EXCEPT `PAYMENT PREFERENCE` which defaults to
  `Primary Insurance` (the PF billing default). Guarantor address is the
  comma-joined `line1, city, state, zip`, emitted only when line1 exists.
  The cells are interpolated raw, so the context guarantees non-empty
  strings — a raw `None` here is a regression
  (`test_payment_never_renders_raw_none`).

## Social history (GOLD §6)

All 17 sub-categories always render (`pack.yaml` carries the empty-state
inventory). The free-text sub-category is wired from `past_medical_history`
where `kind` starts "social"; smoking from the tobacco social-history
observation; gender identity / sexual orientation from the patient. The
remaining sub-categories fall to their documented empty-state strings (no EHI
mapping). SOCIAL HISTORY (FREE-TEXT), GENDER IDENTITY and SEXUAL ORIENTATION
have no "RECORDED" label per the originals.

## Vitals (GOLD §8)

- Display order: Height, Weight, BMI, BMI Percentile, Blood Pressure,
  Temperature, Pulse, Respiratory rate, O2 Saturation, Pain, Head Circumference.
- Systolic + Diastolic combine into one `Blood Pressure` `{sys}/{dia}` row.
- LOINC → label map keys on BOTH the predecessor's primary codes and the modern
  sibling aliases (`core.codes.VITALS`), so a vital charted under either edition
  lands on the right row.
- Vitals flowsheet: PRIOR encounters only (strictly `< current DOS`), most
  recent 10 columns, a merged "Vitals" label row (single colspan cell, no
  spurious vertical dividers — sprint-4 bug).
- Dates `MM/DD/YY`; times `h:mm AM/PM` (no leading zero) in Eastern.

## Addenda (GOLD §10)

Conditional, rendered after Care plan before the logo. 4-col grey-header table
ADDENDUM | STATUS | SOURCE | DATE/TIME (32/32/12/24%). Body + status preserve
newlines (`white-space: pre-line`) so multi-line scores stack. STATUS =
`{AmendmentStatus} by {Author}\n{Credential}`. DATE/TIME =
`MM/DD/YYYY hh:mm am/pm` (lowercase am/pm, zero-padded hour).

## Footer flex truncation fix (GOLD §1)

The original footer is a 2-column flex box: the URL claims the remaining space
after the page number with `flex: 1 1 auto; min-width: 0` — **NOT
`max-width: 80%`**. The 80% box overflowed ~18% of PDFs (a 188-char URL in 438pt
at 7px Arial), clipping the URL to `/soap-` with `text-overflow: ellipsis`
(sprint-5 bug). Font sizes: 7px URL / 9px page number.

**DIVERGENCE / follow-up (running header & footer not yet emitted).** The PF
running header (print-timestamp left, encounter-id right) and footer (URL +
page number with the flex fix) are injected by Playwright's
`page.pdf(header_template=…, footer_template=…)`. The current
`reconstruct/chromium.ChromiumRenderer` does not yet pass those templates, and
the renderer/engine are an untouched-unless-required invariant for this issue.
So this pack ships the footer-flex rule + the **synthetic** footer URL
(`tokens.footer_url` = `https://example.com/encounter/soap-note`) as documented
data, and emitting the running header/footer is recorded as a backwards-
compatible renderer-contract follow-up. The page body (incl. the bottom-right
logo) renders fully today.

## Filename rule (GOLD §11)

The predecessor's full convention is
`{OUTPUT_ROOT}/{First Last}/{MM-DD-YYYY}_{abbreviate_cc(ChiefComplaint)}.pdf`
(a per-patient subdirectory + a 26-pattern, 50-char-capped chief-complaint
abbreviation). The current `LoadedPack`/engine filename contract exposes only
`{family}/{given}/{dos}/{type}`, applies `_safe_name` (which strips path
separators), computes no abbreviated-CC token, and creates no per-patient parent
directory. **DIVERGENCE / follow-up:** rather than hack the engine for one pack,
this pack ships `{given}_{family}_{dos}_{type}.pdf` (the closest the contract
supports, with the same-day `guid_suffix` collision defense carried) and the §11
rule is recorded as a backwards-compatible engine extension (a context-supplied
filename-field hook + parent-dir creation) for a follow-up.

## SOAP HTML (GOLD §9)

`sanitize_soap_html()` (already applied at ingest in `sources/pf_tebra`)
unescapes `\\n`, converts stray `\n` → `<br>` outside block tags, removes empty
`<p>/<div>/<hN>`, and wraps the result in `.pf-rich-text`. The pack renders
`NoteSection.html` with Jinja `| safe` (it is sanitized, trusted source HTML).

---

## Redactions (real identity → synthetic, in this port)

- **PF logo** (real vendor mark, embedded base64 data-URI at template line ~1253
  of the original) → a neutral synthetic SVG placeholder
  (`assets/placeholder_logo.svg`), wired via `tokens.logo_data_uri` /
  `tokens.logo_asset` with an operator-replacement slot. The
  `logo_data_uri` override accepts **inline `data:` URIs only** — an
  http/https/file URL would make Chromium fetch it while rendering PHI, so
  anything else falls back to the placeholder. Local image files go through
  `logo_asset` (read + base64-inlined at context build).
- **Footer URL** (real `static.practicefusion.com/.../{practice-group-guid}/
  encounter/{guid}/soap-note` tenant path, GOLD §1) → synthetic
  `https://example.com/encounter/soap-note` (`tokens.footer_url`).
- **Provider credentials** (4 real provider GUID → credential strings,
  GOLD §2 "Provider credentials (NOT in EHI — hand-mapped)") → NOT shipped; the
  canonical `Practitioner.credential` field is populated from synthetic
  fixtures / operator site-overrides, never a hard-coded real map.
- **Facility name / address / phone / fax** — rendered FROM the (synthetic)
  `facilities.tsv` data, not embedded in the template; the fixture's facility is
  "Example Family Medicine".
- **Provider / patient names** — rendered from the synthetic fixtures
  (e.g. "Paige Providerson", "Ada Q Fixture"), never the real clinic's people.
