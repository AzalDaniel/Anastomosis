"""The draft: write a loadable template pack from a :class:`PackAnalysis`.

Writes ``pack.yaml``, ``template.html`` (mirrors ``generic_soap``'s block
shape and variable names so the real engine renders it unchanged, with
inferred design tokens as CSS custom properties), ``context.py``
(delegates to ``generic_soap``'s builder), ``DRAFT.md``, and — only when
a page was recognized — ``OCR_EVIDENCE.md``. A draft is a STARTING POINT,
not a finished pack; the operator compares the rendered preview against
a real sample and edits ``template.html``. Sample text is quarantined to
``UNPLACED.txt`` only (RULES.md 5); OCR provenance is marked in every
artifact a person opens. Deterministic: the same analysis produces
byte-identical files."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

from anastomosis.core.output import secure_output_dir

from .evidence import AMBIGUOUS, IMAGE_ONLY, MIXED, MIXED_EVIDENCE, LayoutEvidence
from .extract import OCR_SPAN_FONT
from .infer import OCR_EVIDENCE_CAVEAT, PackAnalysis, PageGeometry, SectionCandidate
from .ocr import NATIVE_OR_SYNTHETIC, NATIVE_TEXT, OCR_OBSERVATION

__all__ = [
    "OCR_EVIDENCE_NAME",
    "SAME_PATIENT_CAVEAT",
    "STATIC_LIST_NOTE",
    "emit_draft_pack",
]

#: The OCR provenance file, written only when something was recognized. An
#: empty one in every pack would train operators to ignore the name — the same
#: reasoning that keeps ``UNPLACED.txt`` conditional.
OCR_EVIDENCE_NAME = "OCR_EVIDENCE.md"

#: How each provenance reads to a person opening the draft.
_PROVENANCE_LABELS = {
    NATIVE_TEXT: "native text",
    NATIVE_OR_SYNTHETIC: "text layer over a scan (may itself be OCR)",
    OCR_OBSERVATION: "OCR observation",
    MIXED_EVIDENCE: "native text and OCR observation (both kept)",
}

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
# Window chosen from observed render output.
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
    "become indistinguishable from template text, and are quarantined as raw "
    "sample text. If the samples were not distinct patients, discard this "
    "draft.\n\n"
    "Distinct patients are NOT on their own enough. A string reaches the "
    "static list by appearing in EVERY sample and by owning a place on the "
    "page that nothing else ever occupies — a good filter, and not a proof. "
    "A value all of your patients happen to share, printed in a fixed cell — "
    "a referring provider, a clinic address, a phone number — sits in that "
    "cell on every chart with no competitor to give it away, and looks exactly "
    "like a label the form printed. Read the quarantined strings below and delete "
    "anything that belongs to a patient rather than to the form."
)

# SAME_PATIENT_CAVEAT says this at length at the top of DRAFT.md; this says it
# again in one sentence, and both emitted surfaces put it directly above the
# list of strings it is about. What sat there before called the list template
# chrome and assured the operator in as many words that it held nothing of the
# patient's — flatly contradicting the caveat four paragraphs up, at the one
# moment they are looking at the strings and deciding what to keep. A caveat
# only works if it is still true where the reader is.
STATIC_LIST_NOTE = (
    "These strings were retained from your samples. Inference is a filter, not "
    "proof of who wrote them: a value all of your patients share in a fixed "
    "cell can pass it. Delete anything here that belongs to a patient rather "
    "than to the form."
)

# Exact sample tokens that can retain a header *slot*. The emitted label is a
# fixed vocabulary item, never a copy of a sample string. Do not widen this to
# a prefix regex: ``Provider: Dr X`` is a value, not a Provider label.
_HEADER_LABEL_FIELDS: tuple[tuple[frozenset[str], str, str, str], ...] = (
    (
        frozenset({"dob", "dob:", "date of birth", "date of birth:"}),
        "Birth date",
        "dob",
        "{{ dob }}",
    ),
    (
        frozenset(
            {
                "provider",
                "provider:",
                "seen by",
                "seen by:",
                "rendering provider",
                "rendering provider:",
            }
        ),
        "Clinician",
        "provider",
        "{{ provider.name if provider else '' }}",
    ),
    (
        frozenset({"patient", "patient:", "name", "name:"}),
        "Patient identifier",
        "patient_name",
        "{{ patient_name }}",
    ),
    (
        frozenset({"sex", "sex:", "gender", "gender:"}),
        "Recorded sex",
        "sex",
        "{{ patient.sex or '' }}",
    ),
    (
        frozenset(
            {
                "date of service",
                "date of service:",
                "dos",
                "dos:",
                "visit date",
                "visit date:",
                "encounter date",
                "encounter date:",
            }
        ),
        "Service date",
        "dos",
        "{{ dos }}",
    ),
    (frozenset({"age", "age:"}), "Patient age", "age", "{{ age or '' }}"),
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
    """The named page format whose dimensions best match the inferred
    geometry: exact (within 3pt) when standard, else the nearest standard
    size by summed distance — Playwright's PDF ``format`` takes named
    sizes only. The exact inferred points are recorded in DRAFT.md."""
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
    """The dominant heading-band fill color as ``#rrggbb``: the
    most-used fill inside the band-tint luminance window
    (:data:`_BAND_LUM_MIN`..:data:`_BAND_LUM_MAX`), since raw counts
    alone would be out-voted by thin border slivers. Falls back to the
    most-used non-white fill, then to generic_soap's default."""
    for usage in analysis.design_tokens.fill_colors:
        if _BAND_LUM_MIN <= _luminance(usage.rgb) <= _BAND_LUM_MAX:
            return usage.hex
    for usage in analysis.design_tokens.fill_colors:
        r, g, b = (usage.rgb >> 16) & 0xFF, (usage.rgb >> 8) & 0xFF, usage.rgb & 0xFF
        if not (r >= _WHITE_THRESHOLD and g >= _WHITE_THRESHOLD and b >= _WHITE_THRESHOLD):
            return usage.hex
    return _DEFAULT_HEADING_FILL


def _body_font(analysis: PackAnalysis) -> str:
    """The inferred body font family, with a CSS generic fallback
    appended. PyMuPDF font names are PostScript-ish
    (``Georgia``, ``ABCDEF+Helvetica``); a subset prefix is stripped and a
    generic family appended so the CSS is valid even without the exact
    face on the render host."""
    raw = analysis.design_tokens.body_font or analysis.type_scale.body_font
    if not raw or raw == OCR_SPAN_FONT:
        # OCR recovers no face (Tesseract's text layer is glyphless): offering
        # "OcrObservation" to a CSS stack would assert otherwise, so fall back
        # to the documented default and let DRAFT.md say the font was not inferred.
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
    """The largest h-role type level's size — the section-band font
    size. Levels are sorted size-descending, so the first h-role level
    is the biggest; falls back to generic_soap's 10.5pt convention."""
    for level in analysis.type_scale.levels:
        if level.role.startswith("h"):
            return level.size
    return _DEFAULT_HEADING_SIZE_PT


def _section_candidates(analysis: PackAnalysis) -> list[SectionCandidate]:
    """High-confidence section candidates in median-y (top-to-bottom)
    order: recurs in >= 2 samples (``_MIN_SECTION_COUNT``). A single
    low-confidence sample clears nothing, so the draft emits no manifest
    sections rather than promote per-patient text (DRAFT.md tells the
    operator to add more samples)."""
    if analysis.low_confidence:
        return []
    return [c for c in analysis.sections if c.count >= _MIN_SECTION_COUNT]


def _classify_static(analysis: PackAnalysis) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Identifies exact known header tokens without reproducing their
    text. Returns ``(placed, unplaced)``: ``placed`` is
    ``(canonical_label, slot, value_expr)`` for known header tokens;
    ``unplaced`` is every other raw string. A token matches at most one
    slot (first entry wins)."""
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
        token = " ".join(text.split()).casefold()
        for exact_tokens, canonical_label, slot, value_expr in _HEADER_LABEL_FIELDS:
            if token in exact_tokens and slot not in used_slots:
                match = (canonical_label, slot, value_expr)
                used_slots.add(slot)
                break
        if match is not None:
            placed.append(match)
        else:
            unplaced.append(text)
    return placed, unplaced


#: Section headings that are PUBLISHED CLINICAL VOCABULARY, not sample content.
#:
#: A recurring heading can be a form's label OR a value every sample happened
#: to share (#200), but "Subjective"/"Allergies" are HL7-published SOAP/C-CDA
#: section names printed on every chart in the country — schema, not PHI.
#: Withholding them would cost the learner its whole purpose while protecting
#: nothing. Matching is exact (the same normalisation inference already
#: applies) and the set is deliberately small and closed, so anything else
#: (e.g. "Assessment by Dr Fixture") stays quarantined.
_PUBLISHED_SECTION_HEADINGS = frozenset(
    {
        # SOAP, the note structure every ambulatory chart uses.
        "subjective",
        "objective",
        "assessment",
        "plan",
        "assessment and plan",
        "chief complaint",
        "history of present illness",
        "review of systems",
        "physical exam",
        "physical examination",
        # C-CDA section titles (the LOINC-coded sections in ccda_codes).
        "allergies",
        "allergies and adverse reactions",
        "medications",
        "problems",
        "problem list",
        "immunizations",
        "results",
        "vital signs",
        "vitals",
        "social history",
        "family history",
        "encounters",
        "procedures",
        "plan of treatment",
        "goals",
        "health concerns",
        "insurance",
        "payers",
        "advance directives",
        "functional status",
        "medical equipment",
        "past medical history",
        "notes",
        "addenda",
    }
)


def _section_key(candidate: SectionCandidate, index: int) -> str:
    """The pack.yaml key for one inferred section: a published heading
    gets a readable key derived from its own words, so the manifest
    reads as the chart does; anything else keeps the positional key."""
    known = published_heading(candidate.text)
    if known is None:
        return f"inferred_section_{index}"
    return "_".join(word for word in re.split(r"\W+", known.casefold()) if word)


def published_heading(text: str) -> str | None:
    """The canonical spelling when ``text`` is published vocabulary, else
    ``None``. Case and punctuation vary by vendor ("SUBJECTIVE:",
    "Subjective"), so the comparison folds both; the returned label is
    the sample's own spelling, which is what the operator recognises."""
    folded = text.strip().strip(":").strip().casefold()
    return text.strip().strip(":").strip() if folded in _PUBLISHED_SECTION_HEADINGS else None


def _quarantined_text(analysis: PackAnalysis) -> list[str]:
    """Every raw sample-derived string the generated pack retains.
    Headings aren't in ``static_text`` (inference separates the
    categories) but share the same provenance and boundary. Fail-closed:
    one sample emits no sample text, including into the quarantine file."""
    if analysis.low_confidence:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for text in (*analysis.static_text, *(candidate.text for candidate in analysis.sections)):
        if published_heading(text) is not None:
            continue  # schema, not sample content — it ships in the pack itself
        if text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _quarantine_line(text: str, evidence: LayoutEvidence) -> str:
    """One quarantine entry, marked when recognition is where it came
    from — per string, not per file, since a batch can be part native
    and part pixels."""
    return f"[OCR] {text}" if evidence.is_ocr_derived(text) else text


def _manifest_ocr_marker(evidence: LayoutEvidence) -> str:
    """The one sentence pack.yaml's description carries when OCR was
    used. Short on purpose (detail belongs in ``OCR_EVIDENCE.md``), but
    never omits the fact — a manifest reading identically whether read
    or recognized hides the difference."""
    if not evidence.review_required:
        return ""
    classes = evidence.class_counts
    recognized = classes[IMAGE_ONLY] + classes[MIXED] + classes[AMBIGUOUS]
    return (
        f" OCR EVIDENCE: {recognized} of {len(evidence.pages)} sample page(s) carried raster"
        f" content and were recognized from images, not read; see {OCR_EVIDENCE_NAME}."
        " Recognized text is layout evidence only and is not clinical truth."
    )


def _conflict_rows(evidence: LayoutEvidence) -> list[str]:
    """One markdown row per held native/OCR overlap — geometry, never
    text: page, region, both boxes, and the engine's own score, nothing
    else."""
    rows = [
        "| page | region | kind | OCR box (pt) | native box (pt) | OCR score |",
        "|---|---|---|---|---|---|",
    ]
    for conflict in evidence.conflicts:
        score = "n/a" if conflict.ocr_confidence is None else f"{conflict.ocr_confidence:.1f}"
        rows.append(
            f"| {conflict.page_index} | {conflict.region_id} | {conflict.kind} "
            f"| {_box(conflict.ocr_bbox_pt)} | {_box(conflict.native_bbox_pt)} | {score} |"
        )
    return rows


def _box(bbox: tuple[float, float, float, float]) -> str:
    return "(" + ", ".join(f"{value:.0f}" for value in bbox) + ")"


def _render_ocr_evidence_file(analysis: PackAnalysis, *, name: str) -> str:
    """``OCR_EVIDENCE.md``: what was recognized, what clashed, what it
    means. Written only when something was recognized; carries counts,
    page classes, geometry and the engine manifest — no recognized text,
    which lives (marked) in the quarantine file."""
    evidence = analysis.evidence
    classes = "\n".join(
        f"- {label}: {count} page(s)" for label, count in evidence.class_counts.items() if count
    )
    manifest = (
        "\n".join(f"- `{key}`: {value}" for key, value in evidence.ocr_manifest)
        or "- (no engine manifest was recorded)"
    )
    caveat = textwrap.fill(OCR_EVIDENCE_CAVEAT, width=76)
    conflicts = (
        "\n".join(_conflict_rows(evidence))
        if evidence.conflicts
        else "- (no native/OCR overlap was found)"
    )
    return f"""# OCR evidence for DRAFT pack: {name}

{caveat}

## What this pack may be used for

Recognized geometry MAY suggest: text-line and word boxes, block adjacency,
columns, repeated header/footer bands, table candidates, spacing, and
page-break evidence.

Recognized text MAY NOT establish: that a value is clinically correct or
complete; that an observed font, weight, color or page image is the source
system's own rendering; or that a higher engine score means higher clinical
reliability. Tesseract writes its text layer glyphless and black — no face,
weight or color survives recognition, so this draft's typography is a
destination choice, not a recovered one.

## Page provenance

{classes}

## Observation counts

- Tokens returned by the engine: {evidence.ocr_token_count}
- Used as layout evidence: {evidence.ocr_accepted_count}
- Below the confidence threshold (retained as a count, not promoted):
  {evidence.below_confidence_count}
- Duplicates of native text (dropped from the layout candidates, counted here):
  {evidence.duplicate_count}
- Native/OCR disagreements (BOTH kept; nothing was resolved):
  {evidence.disagreement_count}

## Held conflicts

Nothing below was resolved. Where the two streams described the same place, the
native object and the recognized token were both kept and the page was held for
review. Boxes are in PDF points; no text appears here by design.

{conflicts}

## Engine manifest

{manifest}
"""


# --------------------------------------------------------------------------- #
# pack.yaml
# --------------------------------------------------------------------------- #


def _yaml_quote(value: str) -> str:
    """A double-quoted YAML scalar. An author's display name is free text."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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
        # Its own top-level key, readable back by a picker (#164).
        f"display: {_yaml_quote(display)}",
        'version: "0.1-draft"',
        "description: >",
        f"  DRAFT pack auto-generated from {analysis.sample_count} sample(s) by",
        f"  'anast pack init --from-samples'. {_oneline(display)}. Review the rendered",
        "  preview against an original sample, edit template.html, and re-render"
        " — fidelity is NOT guaranteed (see DRAFT.md)." + _manifest_ocr_marker(analysis.evidence),
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
    for index, candidate in enumerate(sections, start=1):
        # Informational row, default off, so a draft never asserts a section the
        # engine cannot yet populate; the raw heading stays only in the quarantine file.
        known = published_heading(candidate.text)
        key = _section_key(candidate, index)
        lines.append(f"  {key}:")
        lines.append(f"    label: {_yaml_scalar(known or f'Inferred section {index}')}")
        lines.append("    default: false")
        named = f"{known!r} " if known else ""
        # Said only when OCR was actually part of this harvest, so a native-text
        # run's description carries no clause that mentions it at all.
        evidence_clause = (
            f"; evidence: {_PROVENANCE_LABELS.get(candidate.provenance, candidate.provenance)}"
            if analysis.evidence.ocr_attempted
            else ""
        )
        description = (
            f"Inferred heading section {index} {named}(role {candidate.role}; "
            f"seen in {candidate.count}/{analysis.sample_count} samples{evidence_clause})"
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
    """Collapse an operator-supplied string to one safe plain-text line:
    a newline or YAML-active leader in --display could otherwise corrupt
    the folded description block and silently re-key the manifest."""
    import re as _re

    return _re.sub(r"[\r\n:#>|&*?!%@`\"']+", " ", value).strip() or "draft pack"


def _yaml_scalar(value: str) -> str:
    """Quote a YAML scalar so commas/colons in inferred text stay literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --------------------------------------------------------------------------- #
# template.html
# --------------------------------------------------------------------------- #


def _render_template_html(analysis: PackAnalysis) -> str:
    """Generates the Jinja2 template, mirroring generic_soap's block
    structure. Loop structure, class names, and context variables are
    intentionally identical to ``packs/generic_soap/template.html`` so
    the engine renders the draft unchanged; only inlined CSS tokens and
    the patient-header label placement differ."""
    placed, _unplaced = _classify_static(analysis)
    body_size = _body_size_pt(analysis)
    heading_size = _heading_size_pt(analysis)

    # Patient-header label fragments for the static strings we could place.
    label_fragments = "\n".join(
        f"    {{% if {slot_guard(slot)} %}} · {_escape_html(label)} {value}{{% endif %}}"
        for label, slot, value in placed
    )

    unplaced_block = _unplaced_comment(_quarantined_text(analysis))

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
    """HTML-escape static label text emitted into markup. Also
    entity-escapes brace pairs so Jinja never sees a delimiter: a static
    string containing ``{{`` or ``{%`` must render as LITERAL text, never
    execute against the render context."""
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
    """A pointer to the quarantine file — never the strings themselves.
    ``template.html`` renders every future chart and every derived pack
    copies it, so a stray patient value surviving in an HTML comment
    there travels forever. The strings live in ONE inert file instead,
    so deleting :data:`UNPLACED_NAME` is a complete remedy."""
    if not unplaced:
        return ""
    return (
        "<!-- UNPLACED STATIC TEXT — see " + UNPLACED_NAME + ".\n"
        "     Raw strings retained from your samples are listed there, not here:\n"
        "     this file renders real\n"
        "     charts and travels with the pack, and a sample-derived string in\n"
        "     it would travel too. Review what you keep and move it from\n"
        f"     {UNPLACED_NAME} into this template where it belongs, and delete\n"
        "     that file when you are done with it.\n"
        "-->\n"
    )


#: The one file a generated pack puts sample-derived text in. Named so that
#: deleting it is an obvious and complete act: an operator who removes it has
#: removed every string the learner carried out of their charts.
UNPLACED_NAME = "UNPLACED.txt"


def _render_unplaced_file(quarantined: list[str], evidence: LayoutEvidence) -> str:
    """The quarantine file: the strings, and why they need reading. Plain
    text on purpose — not Jinja, YAML or Markdown, so nothing renders or
    imports it; it exists to be read once by a person and then deleted."""
    note = textwrap.fill(STATIC_LIST_NOTE, width=76)
    body = "\n".join(_quarantine_line(text, evidence) for text in quarantined)
    ocr_note = (
        "\nLines marked [OCR] were RECOGNIZED from a page image, not read from\n"
        "the document. They are layout evidence: treat every character as\n"
        f"unverified and check it against the original page. See {OCR_EVIDENCE_NAME}.\n"
        if evidence.review_required
        else ""
    )
    return (
        "UNPLACED STATIC TEXT\n"
        "====================\n\n"
        "These are raw strings retained from your samples, including static text\n"
        "and inferred heading candidates. The generator does not reproduce them\n"
        f"in the working pack files.\n{ocr_note}\n"
        f"{note}\n\n"
        "Move what belongs to the form into template.html, then delete this\n"
        "file. It is the only file in this pack carrying text taken from your\n"
        "samples, so deleting it is the whole job.\n\n"
        "--------------------------------------------------------------------\n"
        f"{body}\n"
    )


def _comment_safe(text: str) -> str:
    """Neutralize text for the UNPLACED comment block: ``-->`` would
    close the comment early, and brace delimiters would be parsed by
    Jinja even inside an HTML comment. Routes through the same
    brace-entity escaping as placed labels."""
    return _escape_html(text).replace("-->", "--&gt;")


# --------------------------------------------------------------------------- #
# context.py
# --------------------------------------------------------------------------- #

# Re-uses generic_soap's build_context verbatim (the generated template
# mirrors that pack's variable contract), so a fix to the shared builder
# flows to drafts too. A triple-quoted literal, not a copied file, since
# this is generated code.
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


def _evidence_one_liner(evidence: LayoutEvidence) -> str:
    """The Provenance bullet's answer to "was any of this recognized?"."""
    if not evidence.review_required:
        return "all native text; no page was recognized from an image"
    return (
        f"{evidence.ocr_accepted_count} recognized token(s) used as layout evidence, "
        f"{evidence.duplicate_count} duplicate(s) and {evidence.disagreement_count} "
        f"disagreement(s) held for review (see {OCR_EVIDENCE_NAME})"
    )


def _draft_ocr_section(analysis: PackAnalysis) -> str:
    """The DRAFT.md OCR block, or nothing for an all-native batch. Sits
    directly under the same-patient caveat (the second thing deciding
    whether this draft can be trusted) and repeats the governing sentence
    verbatim rather than pointing elsewhere: the reader may see nothing
    else."""
    evidence = analysis.evidence
    if not evidence.review_required:
        return ""
    caveat = textwrap.fill(OCR_EVIDENCE_CAVEAT, width=76)
    classes = ", ".join(
        f"{label} {count}" for label, count in evidence.class_counts.items() if count
    )
    return f"""
## OCR evidence (read this second)

{caveat}

- Sample pages by kind: {classes}
- Recognized tokens: {evidence.ocr_token_count}
  ({evidence.ocr_accepted_count} used, {evidence.below_confidence_count} below threshold)
- Held for review, unresolved: {evidence.duplicate_count} duplicate(s) of native
  text, {evidence.disagreement_count} native/OCR disagreement(s)
- Full detail, page classes and the engine manifest: `{OCR_EVIDENCE_NAME}`
- Strings marked `[OCR]` in `{UNPLACED_NAME}` came from recognition
"""


def _render_draft_md(analysis: PackAnalysis, *, name: str, display: str) -> str:
    geom = analysis.page_geometry
    sections = _section_candidates(analysis)
    quarantined = _quarantined_text(analysis)

    confidence = (
        "LOW — only a single sample was analyzed; the static/per-patient split "
        "cannot be made, so no heading sections were promoted. Re-run with "
        "three or more DISTINCT-patient samples."
        if analysis.low_confidence
        else f"derived from {analysis.sample_count} samples; "
        f"{len(sections)} heading section(s) cleared the confidence gate "
        f"(recurring in >= {_MIN_SECTION_COUNT} samples)."
    )

    # Same rule as the manifest description: the evidence clause appears only
    # on a harvest that actually asked an engine.
    def _section_line(index: int, c: SectionCandidate) -> str:
        line = f"- Inferred section {index} — {c.role}, seen in {c.count}/{analysis.sample_count} samples"
        if not analysis.evidence.ocr_attempted:
            return line
        return f"{line}, evidence: {_PROVENANCE_LABELS.get(c.provenance, c.provenance)}"

    section_lines = (
        "\n".join(_section_line(index, c) for index, c in enumerate(sections, start=1))
        or "- (none cleared the confidence gate)"
    )
    if analysis.low_confidence:
        # _quarantined_text withholds everything on one sample, so an empty list
        # here does not mean everything was placed — it means nothing was
        # written at all. Saying "nothing is dropped" over that would be the
        # exact inversion of the truth, so this branch says what happened.
        unplaced_note = (
            "Nothing was written. One sample cannot separate the form from the "
            "patient — every string in it recurs trivially — so no "
            "sample-derived text was written into this pack at all. That "
            "is a deliberate withholding, not a report that there was nothing "
            "to place. Re-run with three or more DISTINCT-patient samples."
        )
        unplaced_lines = "- (withheld — see above)"
    else:
        unplaced_note = (
            f"Every raw string retained from your samples is in `{UNPLACED_NAME}`, "
            "including static strings and inferred headings. "
            f"{STATIC_LIST_NOTE} The generator uses only fixed labels and numbered "
            "placeholders elsewhere, so deleting that file removes all raw "
            "sample-derived text from this pack."
        )
        unplaced_lines = (
            f"- see `{UNPLACED_NAME}` ({len(quarantined)} raw sample string(s) to review)"
            if quarantined
            else "- (no raw sample text was retained)"
        )
    unplaced_note = textwrap.fill(unplaced_note, width=76)

    return f"""# DRAFT pack: {name}

> {display}

**This is a DRAFT, not a finished pack.** It was auto-generated from
{analysis.sample_count} sample PDF(s) by `anast pack init --from-samples`. The
layout learner recovers roughly 60-70% of a pack deterministically; the rest
is a human's job. **Fidelity to your originals is NOT claimed** — treat the
output as a starting point.

## Same-patient caveat (read this first)

{SAME_PATIENT_CAVEAT}
{_draft_ocr_section(analysis)}
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
- Layout evidence: {_evidence_one_liner(analysis.evidence)}

## Inferred heading sections

{section_lines}

## Sample-text quarantine

{unplaced_note}

{unplaced_lines}

## Next steps

1. **Review side-by-side.** Render a preview (`--render-preview`, or
   `anast pipeline run … --pack {name} --pack-dir <this dir's parent>` —
   passing `--pack-dir` opts into trusting this draft's code) and compare
   the rendered PDF in `preview/` to an original sample.
2. **Review `UNPLACED.txt`, then edit `template.html`.** Reposition text you
   keep, wire any inferred heading sections into real loops, and adjust the inlined design
   tokens (CSS custom properties in `:root`).
3. **Re-render** and repeat until the preview matches your sample.
"""


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def emit_draft_pack(analysis: PackAnalysis, *, name: str, display: str, out_dir: Path) -> Path:
    """Writes a loadable draft template pack and returns its directory:
    ``pack.yaml``, ``template.html``, ``context.py``, ``DRAFT.md`` under
    ``<out_dir>/<name>/``, loadable via ``discover_packs``. Deterministic:
    the same ``analysis``/``name``/``display`` produce byte-identical
    files; ``name`` must be manifest-safe (the CLI validates it first)."""
    # Hardened like anything else that can hold PHI, because it can. This
    # module's own caveat says so: hand the tool three copies of ONE patient's
    # chart and that patient's values recur in 100% of samples and are
    # indistinguishable from template text. Raw strings stay in the quarantine,
    # but the generated directory still requires the same owner-only handling.
    pack_dir = secure_output_dir(out_dir / name)
    (pack_dir / "pack.yaml").write_text(
        _render_pack_yaml(analysis, name=name, display=display), encoding="utf-8"
    )
    (pack_dir / "template.html").write_text(_render_template_html(analysis), encoding="utf-8")
    (pack_dir / "context.py").write_text(_CONTEXT_PY, encoding="utf-8")
    (pack_dir / "DRAFT.md").write_text(
        _render_draft_md(analysis, name=name, display=display), encoding="utf-8"
    )
    # The quarantine, and only when there is something to quarantine: an empty
    # UNPLACED.txt in every pack would train operators to ignore the name.
    quarantined = _quarantined_text(analysis)
    if quarantined:
        (pack_dir / UNPLACED_NAME).write_text(
            _render_unplaced_file(quarantined, analysis.evidence), encoding="utf-8"
        )
    # Written only when a page was recognized, for the same reason the
    # quarantine file is conditional: a file that is always there and usually
    # empty is a file nobody reads.
    if analysis.evidence.review_required:
        (pack_dir / OCR_EVIDENCE_NAME).write_text(
            _render_ocr_evidence_file(analysis, name=name), encoding="utf-8"
        )
    return pack_dir
