"""Tests for the offline archive deliverer.

The archive must be browsable from a ``file://`` URL with zero outbound
network requests, and the FHIR Bundle JSON it emits per patient must
round-trip back to a canonical PatientRecord. Synthetic PF fixture data
only (the fixture is the standard one driven from
``tests/fixtures/pf_tebra_v9``)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import anastomosis.sources.pf_tebra  # noqa: F401 — registers the source adapter
from anastomosis.core.fhir import from_bundle
from anastomosis.core.model import PatientRecord
from anastomosis.deliver.archive import ArchiveDeliverer
from anastomosis.deliver.archive.archive import _patient_prefix
from anastomosis.sources import get_source

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"


@pytest.fixture
def records() -> list[PatientRecord]:
    return list(get_source("pf-tebra").load(FIXTURE))


def _fake_pdfs(records: list[PatientRecord], pdfs_dir: Path) -> list[Path]:
    """Materialize one fake-but-valid PDF per encounter using the same name
    pattern the engine produces, so the deliverer's PDF lookup has files to
    match against. ``b"%PDF-1.7 fake"`` is the same shape used in
    ``test_engine.py``'s FakeRenderer."""
    pdfs_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    seen: set[str] = set()
    for record in records:
        prefix = _patient_prefix(record.patient)
        if not prefix:
            continue
        for encounter in record.encounters:
            dos = (
                encounter.date_of_service.strftime("%m-%d-%Y")
                if encounter.date_of_service
                else "undated"
            )
            note_type = re.sub(r"[^A-Za-z0-9_-]+", "_", (encounter.note_type or "note")).strip("_")
            name = f"{prefix}{dos}_{note_type}.pdf"
            if name in seen:
                suffix = encounter.id.replace("-", "")[:8]
                name = f"{prefix}{dos}_{note_type}-{suffix}.pdf"
            seen.add(name)
            path = pdfs_dir / name
            path.write_bytes(b"%PDF-1.7 fake\n")
            written.append(path)
    return written


def test_archive_emits_browsable_tree(tmp_path: Path, records: list[PatientRecord]) -> None:
    pdfs_dir = tmp_path / "charts"
    written_pdfs = _fake_pdfs(records, pdfs_dir)
    assert written_pdfs, "fixture must produce at least one fake pdf"

    out = tmp_path / "archive"
    deliverer = ArchiveDeliverer(generator="anastomosis test")
    result = deliverer.deliver(records, pdfs_dir, out)

    # Top-level structure.
    assert result.out_dir == out
    assert result.patient_count == len(records)
    assert (out / "index.html").is_file()
    assert (out / "index.json").is_file()
    assert (out / "README.txt").is_file()
    assert (out / "assets" / "anast.css").is_file()
    assert (out / "assets" / "anast-index.js").is_file()
    assert (out / "_PHI_WARNING_README.txt").is_file()  # secure_output_dir guarantee
    # LICENSES directory should carry the NOTICE about asset provenance.
    assert (out / "LICENSES" / "NOTICE.txt").is_file()

    # Per-patient structure.
    for record in records:
        patient_dir = out / "patients" / record.patient.id
        assert (patient_dir / "index.html").is_file()
        assert (patient_dir / "bundle.json").is_file()
        # At least one encounter HTML per patient.
        enc_files = list((patient_dir / "encounters").glob("*.html"))
        assert enc_files, f"no encounter pages for {record.patient.id}"

    # Index manifest mentions every patient.
    manifest = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert {entry["id"] for entry in manifest} == {r.patient.id for r in records}
    for entry in manifest:
        assert entry.get("display_name")
        assert entry["encounter_count"] >= 0
        assert "search" in entry  # the haystack used by the search bootstrap


def test_archive_html_is_self_contained(tmp_path: Path, records: list[PatientRecord]) -> None:
    pdfs_dir = tmp_path / "charts"
    _fake_pdfs(records, pdfs_dir)
    out = tmp_path / "archive"
    ArchiveDeliverer(generator="anastomosis test").deliver(records, pdfs_dir, out)

    forbidden = (
        "https://",
        "http://",
        "//cdn",
        'src="//',
        "@import url(",
        "fonts.googleapis",
        "cdnjs",
        "unpkg.com",
        "jsdelivr.net",
    )
    html_files = list(out.rglob("*.html"))
    assert html_files, "archive must emit HTML"
    for html in html_files:
        text = html.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{html.relative_to(out)} references {needle!r}"
        # Every emitted HTML must declare the CSP meta tag.
        assert 'http-equiv="Content-Security-Policy"' in text, (
            f"missing CSP meta tag in {html.relative_to(out)}"
        )
        assert "default-src 'none'" in text
        assert "connect-src 'none'" in text


def test_archive_inline_json_is_data_not_script(
    tmp_path: Path, records: list[PatientRecord]
) -> None:
    pdfs_dir = tmp_path / "charts"
    _fake_pdfs(records, pdfs_dir)
    out = tmp_path / "archive"
    ArchiveDeliverer(generator="anastomosis test").deliver(records, pdfs_dir, out)

    index_html = (out / "index.html").read_text(encoding="utf-8")
    # The inline JSON block must be present and parse as JSON.
    pattern = re.compile(
        r'<script type="application/json" id="anast-index">(?P<body>.*?)</script>',
        re.DOTALL,
    )
    match = pattern.search(index_html)
    assert match is not None, "expected an inline application/json data block"
    payload = match.group("body").replace("<\\/", "</")
    parsed = json.loads(payload)
    assert isinstance(parsed, list)
    assert {entry["id"] for entry in parsed} == {r.patient.id for r in records}

    # No other inline executable <script> blocks: every <script> tag must
    # either be type=application/json (data) or have a src= attribute
    # (self-served file, governed by CSP).
    script_re = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
    for attrs_match in script_re.finditer(index_html):
        attrs = attrs_match.group(1)
        is_json = 'type="application/json"' in attrs
        has_src = re.search(r"\bsrc\s*=", attrs) is not None
        assert is_json or has_src, f"inline executable <script> in index.html: <script{attrs}>"

    # Per-patient pages and per-encounter pages must contain ZERO scripts.
    for html in out.rglob("patients/**/*.html"):
        text = html.read_text(encoding="utf-8")
        assert "<script" not in text, f"unexpected <script> in {html.relative_to(out)}"


_ROUND_TRIP_LIST_FIELDS = (
    "encounters",
    "observations",
    "conditions",
    "allergies",
    "medications",
    "prescriptions",
    "immunizations",
    "family_history",
    "past_medical_history",
    "advance_directives",
    "health_concerns",
    "goals",
    "devices",
    "lab_orders",
    "coverages",
    "documents",
)


def _dumps(models: list) -> list[dict]:  # type: ignore[type-arg]
    return [m.model_dump(mode="json", exclude={"provenance"}) for m in models]


def test_archive_bundle_round_trips(tmp_path: Path, records: list[PatientRecord]) -> None:
    """Read every patient's bundle.json back through ``from_bundle`` and
    confirm the canonical record round-trips (same per-field contract that
    ``test_fhir.test_round_trip_is_lossless`` enforces on the export side)."""
    pdfs_dir = tmp_path / "charts"
    _fake_pdfs(records, pdfs_dir)
    out = tmp_path / "archive"
    ArchiveDeliverer().deliver(records, pdfs_dir, out)

    for source_record in records:
        bundle_path = out / "patients" / source_record.patient.id / "bundle.json"
        assert bundle_path.is_file()
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        restored = from_bundle(bundle)
        assert restored.patient.model_dump(mode="json", exclude={"provenance"}) == (
            source_record.patient.model_dump(mode="json", exclude={"provenance"})
        ), f"patient mismatch for {source_record.patient.id}"
        for field in _ROUND_TRIP_LIST_FIELDS:
            assert _dumps(getattr(restored, field)) == _dumps(getattr(source_record, field)), (
                f"{field} mismatch for {source_record.patient.id}"
            )


def test_archive_pdfs_attributed_to_their_patient(
    tmp_path: Path, records: list[PatientRecord]
) -> None:
    """A PDF named after one patient must never appear under a different
    patient's directory (the cross-leak that motivates per-patient slots)."""
    pdfs_dir = tmp_path / "charts"
    _fake_pdfs(records, pdfs_dir)
    out = tmp_path / "archive"
    ArchiveDeliverer().deliver(records, pdfs_dir, out)

    for record in records:
        prefix = _patient_prefix(record.patient)
        if not prefix:
            continue
        patient_pdfs_dir = out / "patients" / record.patient.id / "pdfs"
        if not patient_pdfs_dir.exists():
            continue
        for pdf in patient_pdfs_dir.glob("*.pdf"):
            assert pdf.name.startswith(prefix), (
                f"{pdf.name} leaked into {record.patient.id}'s pdfs/"
            )


def test_archive_handles_missing_pdfs_dir(tmp_path: Path, records: list[PatientRecord]) -> None:
    """A run without rendered PDFs still produces the browsable archive —
    bundle.json + per-patient HTML, just no chart links."""
    out = tmp_path / "archive"
    result = ArchiveDeliverer().deliver(records, None, out)
    assert result.pdf_count == 0
    for record in records:
        assert (out / "patients" / record.patient.id / "bundle.json").is_file()


def test_archive_index_json_search_haystack_is_lowercased(
    tmp_path: Path, records: list[PatientRecord]
) -> None:
    out = tmp_path / "archive"
    ArchiveDeliverer().deliver(records, None, out)
    manifest = json.loads((out / "index.json").read_text(encoding="utf-8"))
    for entry in manifest:
        assert entry["search"] == entry["search"].lower(), (
            "search haystack must be lowercased for case-insensitive matching"
        )
