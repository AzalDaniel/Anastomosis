"""The draft: write a loadable template pack from a :class:`PackAnalysis`.

This is the third packgen stage (M3b). :mod:`extract` harvested the spans and
drawings, :mod:`infer` distilled them into a :class:`PackAnalysis`; this module
turns that analysis into a pack directory the *real* reconstruction engine can
load and render with **no engine changes**:

    <out_dir>/<name>/
      pack.yaml      — manifest (page geometry, sections, inferred tokens)
      template.html  — generated Jinja2, mirroring generic_soap's block shape
      context.py     — delegates to the generic_soap context builder
      DRAFT.md       — provenance + the same-patient caveat + next steps

A draft is a STARTING POINT, never a finished pack — perfect fidelity is
explicitly *not* claimed (``DRAFT.md`` says so, and the wizard echoes it). The
operator reviews the rendered preview side-by-side with a real sample, edits
``template.html``, and re-renders.

Design choices, grounded in the contracts read for item 15:

* The emitted ``context.py`` re-uses ``generic_soap``'s ``build_context`` rather
  than generating a bespoke one. The generated ``template.html`` therefore
  mirrors ``generic_soap``'s exact loop structure and variable names
  (``note_sections``, ``vitals``, ``addenda``, ``coverages``, ``social_history``,
  ``facility``, ``patient``…), so the engine renders it unchanged. Inferred
  design tokens are inlined as CSS custom properties; what differs from the
  built-in pack is the *look* (tokens), not the data contract.
* **Losslessness**: every static-classified string is placed. Strings that map
  to a known model field become patient-header labels; the rest are emitted
  verbatim inside an ``UNPLACED STATIC TEXT`` HTML comment so nothing is
  silently dropped — the operator positions them by hand.
* **PHI**: only static-classified text (recurring across a supermajority of
  samples — template labels/headings by construction) ever reaches the emitted
  files. Per-patient values never recur and never reach here. The same-patient
  caveat from :mod:`infer` is restated in ``DRAFT.md``.
* **Determinism**: the same :class:`PackAnalysis` produces byte-identical files
  (sorted keys, fixed float formatting, deterministic section ordering).
"""

from __future__ import annotations

import re
from pathlib import Path

from .infer import PackAnalysis, PageGeometry, SectionCandidate

__all__ = [
    "SAME_PATIENT_CAVEAT",
    "emit_draft_pack",
]

# Points per inch — page geometry arrives in points, the manifest wants inches.
_PT_PER_IN = 72.0
# Round emitted margins/sizes to this inch increment (the spec's 0.05in).
_INCH_STEP = 0.05

# A SectionCandidate is confident enough to seed a manifest section when it
# recurs across more than one sample. With a single sample everything recurs
# trivially (count == 1, low_confidence) — those are kept but flagged, never
# silently promoted to high-confidence sections.
_MIN_SECTION_COUNT = 2

# A heading band is a LIGHT shaded tint: luminance just below pure white, never
# a mid/dark gray (those are rules and table borders, not bands). generic_soap's
# band #f1f1f1 sits at luminance ~241; its #bbbbbb cell borders at ~187 and its
# #1a1a1a header rule at ~26. We pick the dominant fill inside the band-tint
# luminance window, which separates the band from the border slivers that the
# PackAnalysis contract (counts only, no area) cannot otherwise distinguish.
# Window chosen from observed render output; documented in the PR decisions.
_BAND_LUM_MIN = 200.0  # below this is a rule/border, not a heading tint
_BAND_LUM_MAX = 252.0  # at/above this is effectively white (no visible band)
# Fallback "non-white" gate for the dominant-by-count path (no band-tint found).
_WHITE_THRESHOLD = 0xF8

# generic_soap's empty-state / token conventions — the documented defaults the
# spec says to fall back to.
_DEFAULT_HEADING_FILL = "#f1f1f1"
_DEFAULT_BODY_FONT = "Georgia, 'Times New Roman', serif"
_DEFAULT_MONO_FONT = "'Courier New', monospace"
_DEFAULT_BODY_SIZE_PT = 11.0
_DEFAULT_HEADING_SIZE_PT = 10.5

# The same-patient caveat, single-sourced from here so DRAFT.md and the wizard
# restate identical wording (the infer.py module docstring is the origin).
SAME_PATIENT_CAVEAT = (
    "These samples MUST be from DIFFERENT patients/encounters. The static/"
    "per-patient text split assumes distinct charts: hand the learner copies "
    "of ONE patient's chart and that patient's values recur in every sample, "
    "become indistinguishable from template text, and WILL be emitted as "
    "labels here. If the samples were not distinct patients, discard this "
    "draft."
)

# Static strings that, after normalization, signal a known patient-header model
# field. Mapping a label here means the generated template renders the field's
# VALUE next to it; everything else is unplaced (commented). Each entry is the
# normalized label -> the header fragment that emits it. The right-hand side
# uses only context variables generic_soap's build_context provides.
_HEADER_LABEL_FIELDS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"^(dob|date of birth)\b", re.IGNORECASE), "dob", "{{ dob }}"),
    (
        re.compile(r"^(provider|seen by|rendering provider)\b", re.IGNORECASE),
        "provider",
        "{{ provider.name if provider else '' }}",
    ),
    (re.compile(r"^(patient|name)\b", re.IGNORECASE), "patient_name", "{{ patient_name }}"),
    (re.compile(r"^(sex|gender)\b", re.IGNORECASE), "sex", "{{ patient.sex or '' }}"),
    (
        re.compile(r"^(date of service|dos|visit date|encounter date)\b", re.IGNORECASE),
        "dos",
        "{{ dos }}",
    ),
    (re.compile(r"^(age)\b", re.IGNORECASE), "age", "{{ age or '' }}"),
)


def _round_to_step(value: float, step: float) -> float:
    """Round ``value`` to the nearest ``step`` (banker's-rounding-free)."""
    return round(round(value / step) * step, 2)


def _inches(points: float) -> str:
    """Points -> an inches string rounded to 0.05in, e.g. ``"0.6in"``.

    Fixed two-decimal formatting keeps the emitted YAML byte-identical across
    runs regardless of float repr drift.
    """
    inches = _round_to_step(points / _PT_PER_IN, _INCH_STEP)
    return f"{inches:.2f}in"


# Standard page sizes in points, with their areas — the manifest's ``page.size``
# is handed verbatim to Playwright's ``page.pdf(format=…)``, which accepts ONLY
# these named formats (not a WxH string). So we ALWAYS emit a named size: an
# exact match when the geometry is standard, else the nearest standard size by
# area, so the draft is guaranteed renderable through the unmodified engine. The
# true inferred point geometry is preserved in DRAFT.md (losslessness).
_KNOWN_SIZES: tuple[tuple[str, float, float], ...] = (
    ("Letter", 612.0, 792.0),
    ("Legal", 612.0, 1008.0),
    ("A4", 595.0, 842.0),
    ("A3", 842.0, 1191.0),
    ("A5", 420.0, 595.0),
)


def _page_size_name(width_pt: float, height_pt: float) -> str:
    """The named page format whose dimensions best match the inferred geometry.

    Exact (within 3pt) when standard; otherwise the nearest standard size by
    summed dimension distance — Playwright's PDF ``format`` takes named sizes
    only, so a draft must never emit a non-renderable ``WxH`` token. The exact
    inferred points are recorded in DRAFT.md instead.
    """
    for name, kw, kh in _KNOWN_SIZES:
        if abs(width_pt - kw) <= 3.0 and abs(height_pt - kh) <= 3.0:
            return name
    if width_pt <= 0.0 or height_pt <= 0.0:
        return "Letter"
    nearest = min(_KNOWN_SIZES, key=lambda s: abs(width_pt - s[1]) + abs(height_pt - s[2]))
    return nearest[0]


def _page_size_is_standard(width_pt: float, height_pt: float) -> bool:
    return any(
        abs(width_pt - kw) <= 3.0 and abs(height_pt - kh) <= 3.0 for _name, kw, kh in _KNOWN_SIZES
    )


def _page_size_note(geom: PageGeometry) -> str:
    """A DRAFT.md note when the emitted named size substitutes for an exotic
    inferred geometry (the renderer takes named formats only)."""
    if _page_size_is_standard(geom.width, geom.height):
        return ""
    return (
        f" (nearest standard size; your samples measured "
        f"{geom.width:.0f}x{geom.height:.0f}pt — the engine renders named sizes "
        "only, so adjust page.size by hand if this is wrong)"
    )


def _luminance(rgb: int) -> float:
    """Rec. 709 relative luminance of a 0xRRGGBB color (0..255)."""
    r, g, b = (rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _heading_fill(analysis: PackAnalysis) -> str:
    """The dominant heading-band fill color as ``#rrggbb``.

    The fill palette arrives most-used-first but carries counts only (no area),
    so the heading band — a few wide, light rects — is out-counted by the many
    thin table-border slivers. A heading band is by design a LIGHT tint, so we
    first take the most-used fill inside the band-tint luminance window
    (:data:`_BAND_LUM_MIN`..:data:`_BAND_LUM_MAX`); that recovers ``#f1f1f1``
    over ``#bbbbbb``/``#1a1a1a``. With no band-tint fill we fall back to the
    most-used merely-non-white fill, then to generic_soap's ``#f1f1f1``.
    """
    for usage in analysis.design_tokens.fill_colors:
        if _BAND_LUM_MIN <= _luminance(usage.rgb) <= _BAND_LUM_MAX:
            return usage.hex
    for usage in analysis.design_tokens.fill_colors:
        r, g, b = (usage.rgb >> 16) & 0xFF, (usage.rgb >> 8) & 0xFF, usage.rgb & 0xFF
        if not (r >= _WHITE_THRESHOLD and g >= _WHITE_THRESHOLD and b >= _WHITE_THRESHOLD):
            return usage.hex
    return _DEFAULT_HEADING_FILL


def _body_font(analysis: PackAnalysis) -> str:
    """The inferred body font family, with a CSS generic fallback appended.

    PyMuPDF font names are PostScript-ish (``Georgia``, ``ABCDEF+Helvetica``);
    we strip any subset prefix and append a generic family so the CSS is valid
    even when the exact face is absent on the render host. Falls back wholesale
    to generic_soap's stack when no body font was inferred.
    """
    raw = analysis.design_tokens.body_font or analysis.type_scale.body_font
    if not raw:
        return _DEFAULT_BODY_FONT
    # Drop a PDF subset prefix like "ABCDEF+".
    family = raw.split("+", 1)[-1].strip()
    if not family:
        return _DEFAULT_BODY_FONT
    generic = "serif" if "serif" in family.lower() or "times" in family.lower() else "sans-serif"
    return f"'{family}', {generic}"


def _body_size_pt(analysis: PackAnalysis) -> float:
    body = analysis.type_scale.body_size
    return body if body is not None else _DEFAULT_BODY_SIZE_PT


def _heading_size_pt(analysis: PackAnalysis) -> float:
    """The largest h-role type level's size — the section-band font size.

    Levels are sorted size-descending, so the first h-role level is the
    biggest. Falls back to generic_soap's 10.5pt band convention.
    """
    for level in analysis.type_scale.levels:
        if level.role.startswith("h"):
            return level.size
    return _DEFAULT_HEADING_SIZE_PT


def _section_candidates(analysis: PackAnalysis) -> list[SectionCandidate]:
    """High-confidence section candidates in median-y (top-to-bottom) order.

    Confidence gate: recurs in >= 2 samples (``_MIN_SECTION_COUNT``). With a
    single low-confidence sample nothing clears the gate, so the draft emits no
    manifest sections rather than promoting per-patient text — the operator is
    told (DRAFT.md) to add more samples. ``analysis.sections`` is already
    median-y sorted, so the relative order is preserved.
    """
    if analysis.low_confidence:
        return []
    return [c for c in analysis.sections if c.count >= _MIN_SECTION_COUNT]


def _section_key(text: str, used: set[str]) -> str:
    """A stable snake_case manifest key for a heading, de-duplicated.

    The manifest's ``sections`` is keyed by identifier (``vitals``,
    ``addenda``); headings are human strings, so we slugify. Collisions get a
    numeric suffix so two headings never silently overwrite one another.
    """
    base = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "section"
    key = base
    suffix = 2
    while key in used:
        key = f"{base}_{suffix}"
        suffix += 1
    used.add(key)
    return key


def _classify_static(analysis: PackAnalysis) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Split static text into placed header labels and unplaced strings.

    Returns ``(placed, unplaced)`` where ``placed`` is a list of
    ``(label, slot, value_expr)`` for strings matching a known patient-header
    field, and ``unplaced`` is every other static string (verbatim), to be
    emitted in the UNPLACED comment block. Section headings are NOT in
    ``static_text`` (infer subtracts them), so they never appear here.
    Determinism: a label matches at most one slot (first pattern wins), each
    slot is placed at most once, and unplaced text keeps input (sorted) order.
    """
    placed: list[tuple[str, str, str]] = []
    unplaced: list[str] = []
    used_slots: set[str] = set()
    if analysis.low_confidence:
        # One sample: "static" is indistinguishable from per-patient values,
        # so NO sample-derived text may reach the emitted files at all (the
        # same PHI gate as summary_lines and the section list). DRAFT.md
        # tells the operator to re-run with more samples.
        return placed, unplaced
    for text in analysis.static_text:
        match = None
        for pattern, slot, value_expr in _HEADER_LABEL_FIELDS:
            if pattern.match(text) and slot not in used_slots:
                match = (text, slot, value_expr)
                used_slots.add(slot)
                break
        if match is not None:
            placed.append(match)
        else:
            unplaced.append(text)
    return placed, unplaced


# --------------------------------------------------------------------------- #
# pack.yaml
# --------------------------------------------------------------------------- #


def _render_pack_yaml(analysis: PackAnalysis, *, name: str, display: str) -> str:
    geom = analysis.page_geometry
    size = _page_size_name(geom.width, geom.height)
    sections = _section_candidates(analysis)
    heading_fill = _heading_fill(analysis)
    body_font = _body_font(analysis)
    body_size = _body_size_pt(analysis)
    heading_size = _heading_size_pt(analysis)

    lines: list[str] = [
        f"name: {name}",
        'version: "0.1-draft"',
        "description: >",
        f"  DRAFT pack auto-generated from {analysis.sample_count} sample(s) by",
        f"  'anast pack init --from-samples'. {_oneline(display)}. Review the rendered",
        "  preview against an original sample, edit template.html, and re-render"
        " — fidelity is NOT guaranteed (see DRAFT.md).",
        "locale: en_US",
        "timezone: America/New_York",
        "page:",
        f"  size: {size}",
        f"  margin_top: {_inches(geom.margin_top)}",
        f"  margin_right: {_inches(geom.margin_right)}",
        f"  margin_bottom: {_inches(geom.margin_bottom)}",
        f"  margin_left: {_inches(geom.margin_left)}",
        "filename:",
        '  pattern: "{family}_{given}_{dos}.pdf"',
        "  collision: guid_suffix",
    ]

    # Sections: always offer the data-driven vitals/addenda toggles the shared
    # context builder + template support, then any inferred heading sections.
    lines.append("sections:")
    lines.extend(
        [
            "  vitals:",
            "    label: Vitals",
            "    default: true",
            "  addenda:",
            "    label: Addenda",
            "    default: true",
            "  insurance:",
            "    label: Insurance / payment information",
            "    default: false",
            "  social_history:",
            "    label: Social history",
            "    default: false",
        ]
    )
    used_keys = {"vitals", "addenda", "insurance", "social_history"}
    for candidate in sections:
        key = _section_key(candidate.text, used_keys)
        # Inferred headings are informational rows (the operator wires them into
        # the template by hand); default off so a draft never asserts a section
        # the engine cannot yet populate.
        lines.append(f"  {key}:")
        lines.append(f"    label: {_yaml_scalar(candidate.text.title())}")
        lines.append("    default: false")
        # The heading text is inferred from samples and may contain YAML-active
        # characters (colons, quotes), so it MUST be a quoted scalar — an
        # unquoted "Foo: bar" heading would emit invalid YAML that fails to load.
        description = (
            f"inferred heading '{candidate.text}'"
            f" (seen in {candidate.count}/{analysis.sample_count} samples)"
        )
        lines.append(f"    description: {_yaml_scalar(description)}")

    lines.append("tokens:")
    lines.append(f'  heading_fill: "{heading_fill}"')
    lines.append(f"  body_font: {_yaml_scalar(body_font)}")
    lines.append(f'  mono_font: "{_DEFAULT_MONO_FONT}"')
    lines.append(f'  body_size: "{body_size:.1f}pt"')
    lines.append(f'  heading_size: "{heading_size:.1f}pt"')
    lines.append("verify_header_fields: [patient_name, dob, dos]")
    return "\n".join(lines) + "\n"


def _oneline(value: str) -> str:
    """Collapse an operator-supplied string to one safe plain-text line.

    Newlines (or YAML-active leaders) in --display could otherwise corrupt the
    folded description block and silently re-key the manifest.
    """
    import re as _re

    return _re.sub(r"[\r\n:#>|&*?!%@`\"']+", " ", value).strip() or "draft pack"


def _yaml_scalar(value: str) -> str:
    """Quote a YAML scalar so commas/colons in inferred text stay literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --------------------------------------------------------------------------- #
# template.html
# --------------------------------------------------------------------------- #


def _render_template_html(analysis: PackAnalysis) -> str:
    """Generate the Jinja2 template, mirroring generic_soap's block structure.

    The loop structure, class names, and context variables are intentionally
    identical to ``packs/generic_soap/template.html`` so the engine renders the
    draft with no changes; only the inlined CSS custom properties (the inferred
    tokens) and the patient-header label placement differ.
    """
    placed, unplaced = _classify_static(analysis)
    body_size = _body_size_pt(analysis)
    heading_size = _heading_size_pt(analysis)

    # Patient-header label fragments for the static strings we could place.
    label_fragments = "\n".join(
        f"    {{% if {slot_guard(slot)} %}} · {_escape_html(label)} {value}{{% endif %}}"
        for label, slot, value in placed
    )

    unplaced_block = _unplaced_comment(unplaced)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{{{{ patient_name }}}} — {{{{ dos }}}}</title>
<style>
  :root {{
    --body-font: {{{{ tokens.get('body_font', 'serif') }}}};
    --mono-font: {{{{ tokens.get('mono_font', 'monospace') }}}};
    --heading-fill: {{{{ tokens.get('heading_fill', '#f1f1f1') }}}};
    --body-size: {{{{ tokens.get('body_size', '{body_size:.1f}pt') }}}};
    --heading-size: {{{{ tokens.get('heading_size', '{heading_size:.1f}pt') }}}};
  }}
  body {{ font-family: var(--body-font); font-size: var(--body-size);
         color: #1a1a1a; margin: 0; }}
  header {{ border-bottom: 2px solid #1a1a1a; padding-bottom: 8px; margin-bottom: 14px; }}
  .facility {{ font-size: 13pt; font-weight: bold; }}
  .facility-meta, .patient-meta {{ font-size: 9.5pt; color: #333; }}
  h2.section {{ background: var(--heading-fill);
               font-size: var(--heading-size); text-transform: uppercase;
               letter-spacing: .04em; padding: 4px 8px; margin: 16px 0 6px;
               page-break-after: avoid; }}
  .section-body {{ padding: 0 8px; }}
  table.vitals {{ border-collapse: collapse; margin: 4px 8px; }}
  table.vitals td, table.vitals th {{ border: 1px solid #bbb; padding: 3px 8px;
                                     font-size: 9.5pt; text-align: left; }}
  .addendum {{ border-left: 3px solid #888; margin: 8px; padding: 4px 10px;
              font-size: 10pt; }}
  .addendum-meta {{ color: #555; font-size: 8.5pt; }}
  footer.sig {{ margin-top: 24px; border-top: 1px solid #1a1a1a; padding-top: 6px;
               font-size: 10pt; page-break-inside: avoid; }}
  .unsigned {{ color: #8a5a00; font-weight: bold; }}
</style>
</head>
<body>
{unplaced_block}<header>
  {{% if facility %}}
    <div class="facility">{{{{ facility.name }}}}</div>
    <div class="facility-meta">
      {{{{ facility.address_line1 }}}}{{% if facility.address_line2 %}}, {{{{ facility.address_line2 }}}}{{% endif %}},
      {{{{ facility.city }}}}, {{{{ facility.state }}}} {{{{ facility.postal_code }}}}
      {{% if facility.phone %}} · Tel {{{{ facility.phone }}}}{{% endif %}}
      {{% if facility.fax %}} · Fax {{{{ facility.fax }}}}{{% endif %}}
    </div>
  {{% endif %}}
  <div class="patient-meta">
    <strong>{{{{ patient_name }}}}</strong>
    {{% if dob %}} · DOB {{{{ dob }}}}{{% endif %}}
    {{% if age %}} ({{{{ age }}}}){{% endif %}}
    {{% if patient.sex %}} · {{{{ patient.sex }}}}{{% endif %}}
    · Date of service: {{{{ dos }}}}
    {{% if encounter.note_type %}} · {{{{ encounter.note_type }}}}{{% endif %}}
{label_fragments}
  </div>
  {{% if encounter.chief_complaint %}}
    <div class="patient-meta">Chief complaint: {{{{ encounter.chief_complaint }}}}</div>
  {{% endif %}}
</header>

{{% for section in note_sections %}}
  <h2 class="section">{{{{ section.title or "Note" }}}}</h2>
  {{# Source note HTML is rendered as authored; print CSS cannot run scripts
     and Chromium renders with no network access to leak to. #}}
  <div class="section-body">{{{{ section.html | safe if section.html else section.text }}}}</div>
{{% endfor %}}

{{% if vitals %}}
  <h2 class="section">Vitals</h2>
  <table class="vitals">
    <tr><th>Measure</th><th>Value</th><th>Unit</th></tr>
    {{% for v in vitals %}}
      <tr><td>{{{{ v.display or v.code }}}}</td><td>{{{{ v.value }}}}</td><td>{{{{ v.unit or "" }}}}</td></tr>
    {{% endfor %}}
  </table>
{{% endif %}}

{{% if social_history %}}
  <h2 class="section">Social history</h2>
  <table class="vitals">
    {{% for o in social_history %}}
      <tr><td>{{{{ o.display }}}}</td><td>{{{{ o.value }}}}</td></tr>
    {{% endfor %}}
  </table>
{{% endif %}}

{{% if coverages %}}
  <h2 class="section">Payment information</h2>
  <table class="vitals">
    <tr><th>Order</th><th>Payer</th><th>Plan</th><th>Type</th><th>Member ID</th></tr>
    {{% for c in coverages %}}
      <tr><td>{{{{ c.priority_label or "" }}}}</td><td>{{{{ c.payer or "" }}}}</td>
          <td>{{{{ c.plan_name or "" }}}}</td><td>{{{{ c.plan_type or c.coverage_type or "" }}}}</td>
          <td>{{{{ c.member_id or "" }}}}</td></tr>
    {{% endfor %}}
  </table>
{{% endif %}}

{{% if addenda %}}
  <h2 class="section">Addenda</h2>
  {{% for addendum in addenda %}}
    <div class="addendum">
      {{{{ addendum.text }}}}
      <div class="addendum-meta">
        {{{{ addendum.status or "" }}}}{{% if addendum.source %}} · {{{{ addendum.source }}}}{{% endif %}}
      </div>
    </div>
  {{% endfor %}}
{{% endif %}}

<footer class="sig">
  {{% if signer and signed_at %}}
    Electronically signed by {{{{ signer.name }}}}{{% if signer.credential %}}, {{{{ signer.credential }}}}{{% endif %}}
    on {{{{ signed_at }}}}
  {{% else %}}
    <span class="unsigned">UNSIGNED NOTE</span>
    {{% if provider %}} · Seen by {{{{ provider.name }}}}{{% endif %}}
  {{% endif %}}
</footer>
</body>
</html>
"""


def slot_guard(slot: str) -> str:
    """The Jinja2 truthiness guard for a placed header slot."""
    guards = {
        "dob": "dob",
        "provider": "provider",
        "patient_name": "patient_name",
        "sex": "patient.sex",
        "dos": "dos",
        "age": "age",
    }
    return guards.get(slot, slot)


def _escape_html(text: str) -> str:
    """HTML-escape static label text emitted into markup.

    Also entity-escapes brace pairs so Jinja never sees a delimiter: a static
    string containing ``{{`` or ``{%`` must render as LITERAL text (it came
    from a sample document), never execute against the render context.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("{{", "&#123;&#123;")
        .replace("{%", "&#123;%")
        .replace("}}", "&#125;&#125;")
        .replace("%}", "%&#125;")
    )


def _unplaced_comment(unplaced: list[str]) -> str:
    """The UNPLACED STATIC TEXT comment block — losslessness made visible.

    Every static string that did not map to a known model field is emitted
    here, verbatim and HTML-comment-safe, so the operator can position it by
    hand. Empty when nothing was unplaced.
    """
    if not unplaced:
        return ""
    body = "\n".join(f"    {_comment_safe(text)}" for text in unplaced)
    return (
        "<!-- UNPLACED STATIC TEXT — position manually.\n"
        "     These strings recurred across your samples (template labels/\n"
        "     boilerplate) but did not map to a known model field. Nothing is\n"
        "     dropped: move each into the template where it belongs.\n"
        f"{body}\n"
        "-->\n"
    )


def _comment_safe(text: str) -> str:
    """Neutralize text for the UNPLACED comment block.

    Two hazards: ``-->`` would close the comment early, and brace delimiters
    would be parsed by Jinja even INSIDE an HTML comment — sample-derived text
    must render as literal characters, never evaluate against the render
    context. Route through the same brace-entity escaping as placed labels.
    """
    return _escape_html(text).replace("-->", "--&gt;")


# --------------------------------------------------------------------------- #
# context.py
# --------------------------------------------------------------------------- #

# The emitted context.py re-uses generic_soap's build_context verbatim: the
# generated template mirrors that pack's variable contract exactly, so there is
# nothing pack-specific to compute. Keeping it a thin re-export (rather than a
# copy) means a fix to the shared builder flows to drafts too, and the loader's
# `build_context` callable requirement is satisfied. A triple-quoted literal,
# not a copied file, honors the no-copy PHI rule (this is generated code).
_CONTEXT_PY = '''"""Context builder for a packgen DRAFT pack.

Auto-generated by ``anast pack init --from-samples`` (packgen.emit). A draft
re-uses the vendor-neutral generic_soap context contract unchanged — the
generated template.html mirrors that pack's variable names — so the real
reconstruction engine renders it with no engine changes. Edit freely once you
start tailoring the draft to your samples.
"""

from __future__ import annotations

from typing import Any

from anastomosis.core.model import Encounter, PatientRecord
from anastomosis.packs.generic_soap.context import build_context as _build_context

__all__ = ["build_context"]


def build_context(
    encounter: Encounter, record: PatientRecord, cfg: dict[str, Any]
) -> dict[str, Any]:
    return _build_context(encounter, record, cfg)
'''


# --------------------------------------------------------------------------- #
# DRAFT.md
# --------------------------------------------------------------------------- #


def _render_draft_md(analysis: PackAnalysis, *, name: str, display: str) -> str:
    geom = analysis.page_geometry
    sections = _section_candidates(analysis)
    _placed, unplaced = _classify_static(analysis)

    confidence = (
        "LOW — only a single sample was analyzed; the static/per-patient split "
        "cannot be made, so no heading sections were promoted. Re-run with "
        "three or more DISTINCT-patient samples."
        if analysis.low_confidence
        else f"derived from {analysis.sample_count} samples; "
        f"{len(sections)} heading section(s) cleared the confidence gate "
        f"(recurring in >= {_MIN_SECTION_COUNT} samples)."
    )

    section_lines = (
        "\n".join(
            f"- `{c.text}` — {c.role}, seen in {c.count}/{analysis.sample_count} samples"
            for c in sections
        )
        or "- (none cleared the confidence gate)"
    )
    unplaced_lines = (
        "\n".join(f"- {t}" for t in unplaced) or "- (all static text mapped to known header fields)"
    )

    return f"""# DRAFT pack: {name}

> {display}

**This is a DRAFT, not a finished pack.** It was auto-generated from
{analysis.sample_count} sample PDF(s) by `anast pack init --from-samples`. The
layout learner recovers roughly 60-70% of a pack deterministically; the rest
is a human's job. **Fidelity to your originals is NOT claimed** — treat the
output as a starting point.

## Same-patient caveat (read this first)

{SAME_PATIENT_CAVEAT}

## Provenance

- Samples analyzed: {analysis.sample_count}
- Confidence: {confidence}
- Page geometry: {geom.width:.0f}x{geom.height:.0f}pt
  (margins L{geom.margin_left:.0f} R{geom.margin_right:.0f}
  T{geom.margin_top:.0f} B{geom.margin_bottom:.0f}pt)
- Emitted page size: `{_page_size_name(geom.width, geom.height)}`{_page_size_note(geom)}
- Heading-band fill: `{_heading_fill(analysis)}`
- Body font: `{_body_font(analysis)}`
- Dropped curves (vector art the harvester skipped): {analysis.dropped_curves}

## Inferred heading sections

{section_lines}

## Unplaced static text

The strings below recurred across your samples (so they are template labels/
boilerplate, not patient data) but did not map to a known model field. They
are preserved verbatim inside an `UNPLACED STATIC TEXT` comment at the top of
`template.html` — nothing is dropped. Move each into the template by hand:

{unplaced_lines}

## Next steps

1. **Review side-by-side.** Render a preview (`--render-preview`, or
   `anast pipeline run … --pack {name} --pack-dir <this dir's parent>` —
   passing `--pack-dir` opts into trusting this draft's code) and compare
   the rendered PDF in `preview/` to an original sample.
2. **Edit `template.html`.** Reposition the unplaced static text, wire any
   inferred heading sections into real loops, and adjust the inlined design
   tokens (CSS custom properties in `:root`).
3. **Re-render** and repeat until the preview matches your sample.
"""


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def emit_draft_pack(analysis: PackAnalysis, *, name: str, display: str, out_dir: Path) -> Path:
    """Write a loadable draft template pack and return its directory.

    Creates ``<out_dir>/<name>/`` containing ``pack.yaml``, ``template.html``,
    ``context.py``, and ``DRAFT.md``. The result loads through
    ``discover_packs([pack_dir.parent], allow_external=True)`` and renders
    through the real engine unchanged. Deterministic: the same ``analysis`` and
    ``name``/``display`` produce byte-identical files.

    ``name`` must be a manifest-safe identifier (the pack name and directory
    name); the caller (CLI) validates it before reaching here.
    """
    pack_dir = out_dir / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "pack.yaml").write_text(
        _render_pack_yaml(analysis, name=name, display=display), encoding="utf-8"
    )
    (pack_dir / "template.html").write_text(_render_template_html(analysis), encoding="utf-8")
    (pack_dir / "context.py").write_text(_CONTEXT_PY, encoding="utf-8")
    (pack_dir / "DRAFT.md").write_text(
        _render_draft_md(analysis, name=name, display=display), encoding="utf-8"
    )
    return pack_dir
