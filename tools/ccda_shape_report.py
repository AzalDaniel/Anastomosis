#!/usr/bin/env python3
"""Run the C-CDA adapter over a real corpus and report what it found, PHI-safely.

The sibling of ``tools/real_export_report.py``, and it exists for the same
reason: the questions that matter about this adapter can only be answered by a
corpus that must never leave the operator's machine. So the corpus stays there
and only the SHAPE of it travels.

Two kinds of finding come out of this, and the second is why it is worth
running:

* **Shapes the parser has never seen.** Every ``xsi:type``, code system OID,
  ``nullFlavor``, template id and section code in the corpus, counted. The
  fixtures pin a handful; a real batch of a few thousand documents has a longer
  tail, and a shape nobody anticipated is where silent loss lives.
* **Facts that arrived and then went nowhere.** The parser can succeed on every
  document and still produce records that render as almost nothing. An
  observation no encounter claims, an encounter with no date, a condition with
  no display — each is a value that survived ingest and will be absent from the
  chart. Those counters are the point; a green parse rate is not.

What may leave this script is enumerated by :func:`_safe` and enforced, not
promised: element and attribute NAMES, OIDs and structural codes, exception
TYPE names, and integers. No cell value, no free text, no filename, no path.
A patient's name cannot be an element name, so the vocabulary itself is the
control.

Usage:
    python tools/ccda_shape_report.py <dir-of-ccda-xml> [--out report.json]
    python tools/ccda_shape_report.py <corpus.zip>      [--out report.json]

Run it beside the same command against ``tests/fixtures/ccda`` and diff the two:
what the real corpus has and the fixtures do not is the work list.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Structural vocabularies. Anything emitted as a string must match one of these
# or be a name the parser itself defines; everything else is a value and stays
# on the operator's machine.
_OID = re.compile(r"^[0-9]+(\.[0-9]+)+$")
# Letters, digits and underscore only — deliberately NARROWER than XML's NCName,
# which also admits dots and hyphens. A real export names its files after the
# patient, and `Specimen_Cora_2023-05-10.xml` is a legal NCName; the first draft
# of this pattern let one straight through, and the test below caught it.
_NCNAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOINC = re.compile(r"^[0-9]{1,6}-[0-9]$")
_TS_SHAPE = re.compile(r"^len[0-9]{1,2}(\+tz)?(\+frac)?$")


def _safe(value: object) -> bool:
    """Whether ``value`` is structural enough to leave the operator's machine.

    Deliberately a whitelist. A new counter that wants to emit something this
    does not recognise should have to argue for it here, in the open, rather
    than slip out inside a dict nobody re-read.
    """
    if isinstance(value, bool) or isinstance(value, int):
        return True
    if not isinstance(value, str):
        return False
    return bool(
        _OID.match(value)
        or _NCNAME.match(value)
        or _LOINC.match(value)
        or _TS_SHAPE.match(value)
        or value in {"", "|", "/"}
    )


def _assert_safe(report: object, where: str = "report") -> None:
    """Walk the finished report and refuse to write anything unvetted."""
    if isinstance(report, dict):
        for key, val in report.items():
            if not _safe(key):
                raise SystemExit(f"REFUSING to emit unsafe key at {where}: {key!r}")
            _assert_safe(val, f"{where}.{key}")
    elif isinstance(report, list):
        for i, val in enumerate(report):
            _assert_safe(val, f"{where}[{i}]")
    elif not _safe(report):
        raise SystemExit(f"REFUSING to emit unsafe value at {where}: {report!r}")


def _ts_shape(raw: str) -> str:
    """A timestamp's PRECISION, with the digits thrown away.

    ``20230510150405.000-0500`` becomes ``len14+tz+frac``. The value is a date a
    patient was seen; the shape is what tells us whether the parser can read it,
    and #241 was exactly a precision the parser mis-segmented.
    """
    text = raw.strip()
    tz = "+tz" if re.search(r"(Z|[+-][0-9]{2}:?[0-9]{2})$", text) else ""
    text = re.sub(r"(Z|[+-][0-9]{2}:?[0-9]{2})$", "", text)
    frac = "+frac" if "." in text else ""
    digits = len(re.sub(r"\D", "", text.split(".")[0]))
    return f"len{digits}{tz}{frac}"


@contextmanager
def _documents(source: Path) -> Iterator[Iterator[tuple[str, bytes]]]:
    """Yield ``(stable_label, xml_bytes)`` for every document in a dir or zip.

    The label is an index, never the filename: a real export names its files
    after the patient, so the name is PHI even when the contents are not read.

    Matched against the adapter's own ``_DOCUMENT_SUFFIXES`` (``.xml``,
    ``.ccd``, ``.ccda``), not a bare ``*.xml`` glob: this tool audits a real
    corpus for the shapes the adapter has never seen, so an instrument with
    the SAME blind spot #384 fixed in the adapter itself would silently drop
    the ``.ccd``/``.ccda`` documents from the very corpus it exists to
    characterize (#384 round two, finding 5).
    """
    from anastomosis.sources.ccda import _DOCUMENT_SUFFIXES

    if source.is_dir():
        paths = sorted(
            p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in _DOCUMENT_SUFFIXES
        )

        def from_dir() -> Iterator[tuple[str, bytes]]:
            for i, p in enumerate(paths):
                yield f"doc{i}", p.read_bytes()

        yield from_dir()
        return
    with zipfile.ZipFile(source) as zf:
        names = sorted(n for n in zf.namelist() if Path(n).suffix.lower() in _DOCUMENT_SUFFIXES)

        def from_zip() -> Iterator[tuple[str, bytes]]:
            for i, n in enumerate(names):
                yield f"doc{i}", zf.read(n)

        yield from_zip()


def _census(xml: bytes, tally: dict[str, Counter[str]]) -> None:
    """Count the structural shapes in one document, without reading any value."""
    from lxml import etree

    root = etree.fromstring(xml)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        local = etree.QName(el).localname
        tally["elements"][local] += 1
        for name, val in el.attrib.items():
            attr = etree.QName(name).localname if name.startswith("{") else name
            tally["attributes"][attr] += 1
            if attr == "type":
                tally["xsi_types"][val] += 1
            elif attr == "nullFlavor":
                tally["null_flavors"][val] += 1
            elif attr == "codeSystem":
                tally["code_systems"][val] += 1
            elif attr == "root" and _OID.match(val):
                tally["template_roots"][val] += 1
            elif attr == "value" and re.fullmatch(r"[0-9]{4,}(\.[0-9]+)?(Z|[+-][0-9:]{4,5})?", val):
                tally["timestamp_shapes"][_ts_shape(val)] += 1
        if local == "code" and el.getparent() is not None:
            parent = etree.QName(el.getparent()).localname
            if parent == "section":
                code = el.get("code")
                if code and _LOINC.match(code):
                    tally["section_codes"][code] += 1


def _record_counters(record: Any) -> Counter[str]:
    """What the parser produced, and — the point — what went nowhere.

    A collection count says ingest worked. The ``_unattributed`` and ``_no_``
    counters say whether the values will reach a chart, which is a different
    question and the one that keeps being answered wrong.

    Every conservation counter is seeded at zero rather than left to appear on
    first increment. A key missing from the returned report reads as "the tool
    did not look"; a key at zero reads as "the tool looked and found none". We
    are asking an operator to send this back as evidence, so the report has to
    be able to say the second thing.
    """
    c: Counter[str] = Counter()
    for name in (
        "encounters",
        "observations",
        "conditions",
        "medications",
        "allergies",
        "immunizations",
        "documents",
        "goals",
        "practitioners",
        "facilities",
    ):
        c[name] = len(getattr(record, name, []) or [])
    for name in (
        "observations_unattributed",
        "observations_dangling_encounter",
        "observations_no_value",
        "encounters_no_date",
        "encounters_no_type",
        "encounters_no_note_body",
        "conditions_no_display",
    ):
        c[name] = 0

    encounter_ids = {e.id for e in record.encounters}
    for obs in record.observations:
        if obs.encounter_id is None:
            c["observations_unattributed"] += 1
        elif obs.encounter_id not in encounter_ids:
            c["observations_dangling_encounter"] += 1
        if obs.value is None:
            c["observations_no_value"] += 1
    for enc in record.encounters:
        if enc.date_of_service is None:
            c["encounters_no_date"] += 1
        if not enc.encounter_type:
            c["encounters_no_type"] += 1
        if not any((s.text or "").strip() for s in enc.sections):
            c["encounters_no_note_body"] += 1
    for cond in record.conditions:
        if not cond.display:
            c["conditions_no_display"] += 1
    c["patient_extension_keys"] = len(record.patient.extensions or {})
    return c


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path, help="directory of C-CDA XML, or a .zip of them")
    ap.add_argument("--out", type=Path, default=Path("ccda_shape_report.json"))
    ap.add_argument("--limit", type=int, default=0, help="stop after N documents (0 = all)")
    args = ap.parse_args()

    from anastomosis.sources.ccda.parser import parse_document

    tally: dict[str, Counter[str]] = {
        k: Counter()
        for k in (
            "elements",
            "attributes",
            "xsi_types",
            "null_flavors",
            "code_systems",
            "template_roots",
            "section_codes",
            "timestamp_shapes",
        )
    }
    parse_errors: Counter[str] = Counter()
    census_errors: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    per_doc_zero: Counter[str] = Counter()
    seen = 0

    import tempfile

    with _documents(args.source) as docs:
        for _label, xml in docs:
            seen += 1
            if args.limit and seen > args.limit:
                seen -= 1
                break
            try:
                _census(xml, tally)
            except Exception as exc:
                census_errors[type(exc).__name__] += 1
            # parse_document takes a path, so give it a temporary one whose name
            # says nothing about the patient.
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td) / "d.xml"
                tmp.write_bytes(xml)
                try:
                    record = parse_document(tmp)
                except Exception as exc:
                    parse_errors[type(exc).__name__] += 1
                    continue
            counters = _record_counters(record)
            totals.update(counters)
            for name in ("encounters", "observations", "conditions", "medications"):
                if counters[name] == 0:
                    per_doc_zero[name] += 1

    parsed = seen - sum(parse_errors.values())
    report: dict[str, object] = {
        "version": 1,
        "documents_seen": seen,
        "documents_parsed": parsed,
        "parse_errors_by_type": dict(parse_errors),
        "census_errors_by_type": dict(census_errors),
        "totals": dict(totals),
        "documents_with_zero": dict(per_doc_zero),
        "shapes": {k: dict(v.most_common()) for k, v in tally.items()},
    }
    _assert_safe(report)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    print(f"documents      : {seen}")
    print(f"parsed         : {parsed}")
    if parse_errors:
        print(f"parse errors   : {dict(parse_errors)}")
    if parsed:
        obs = totals["observations"]
        una = totals["observations_unattributed"]
        print(f"observations   : {obs} ({una} attached to no encounter)")
        undated = totals["encounters_no_date"]
        print(f"encounters     : {totals['encounters']} ({undated} with no date)")
        print(f"docs with zero : {dict(per_doc_zero)}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
