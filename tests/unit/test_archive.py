"""Tests for the offline archive deliverer.

The archive must be browsable from a ``file://`` URL with zero outbound
network requests, and the FHIR Bundle JSON it emits per patient must
round-trip back to a canonical PatientRecord. Synthetic PF fixture data
only (the fixture is the standard one driven from
``tests/fixtures/pf_tebra_v9``)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

import anastomosis.sources.pf_tebra  # noqa: F401 — registers the source adapter
from anastomosis.core.fhir import from_bundle
from anastomosis.core.logutil import safe_log_id
from anastomosis.core.model import Patient, PatientRecord
from anastomosis.deliver.archive import ArchiveDeliverer
from anastomosis.deliver.render_index import RenderEntry, RenderIndex
from anastomosis.sources import get_source

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"


@pytest.fixture
def records() -> list[PatientRecord]:
    return list(get_source("pf-tebra").load(FIXTURE))


def _engine_prefix(patient: Patient) -> str:
    """The engine's ``{family}_{given}_`` filename prefix.

    Used only by the test's :func:`_fake_pdfs` helper to mimic the engine's
    naming pattern; production attribution is by the render-index sidecar
    the engine writes, not by this prefix (which would cross-leak between
    same-named patients).
    """
    family = re.sub(r"[^A-Za-z0-9_-]+", "_", (patient.family_name or "").strip()).strip("_")
    given = re.sub(r"[^A-Za-z0-9_-]+", "_", (patient.given_name or "").strip()).strip("_")
    if not (family and given):
        return ""
    return f"{family}_{given}_"


def _fake_pdfs(records: list[PatientRecord], pdfs_dir: Path) -> list[Path]:
    """Materialize one fake-but-valid PDF per encounter using the same name
    pattern the engine produces AND write the render-index sidecar so the
    deliverer can attribute each PDF to its owning patient. ``b"%PDF-1.7
    fake"`` is the same shape used in ``test_engine.py``'s FakeRenderer.
    """
    pdfs_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    entries: list[RenderEntry] = []
    seen: set[str] = set()
    for record in records:
        prefix = _engine_prefix(record.patient)
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
            entries.append(
                RenderEntry(
                    pdf=name,
                    patient_id=record.patient.id,
                    encounter_id=encounter.id,
                )
            )
    RenderIndex.from_entries(entries).write(pdfs_dir)
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
    "goals",
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
        prefix = _engine_prefix(record.patient)
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


def test_archive_long_ids_stay_writable_and_linked(tmp_path: Path) -> None:
    """A source id far longer than the filesystem allows must still produce a
    browsable archive: the patient directory and every encounter page are
    written, and the link on the patient page resolves to the file that was
    actually created. Unbudgeted, this raised ``OSError``/``FileNotFoundError``
    mid-delivery; a link that pointed at nothing would be just as bad — a chart
    the operator cannot reach.
    """
    from datetime import date

    from anastomosis.core.model import Encounter, Patient, PatientRecord

    # Synthetic, absurdly long ids (the shape a vendor export with a composite
    # key can produce). The two encounters differ ONLY past the cap.
    long_pid = "feedface-0000-0000-0000-0000000000aa" + "z" * 300
    enc_base = "feedface-e000-0000-0000-0000000000aa" + "y" * 300
    record = PatientRecord(
        patient=Patient(id=long_pid, family_name="Fixture", given_name="Ada"),
        encounters=[
            Encounter(id=enc_base + "-one", patient_id=long_pid, date_of_service=date(2023, 5, 10)),
            Encounter(id=enc_base + "-two", patient_id=long_pid, date_of_service=date(2023, 6, 10)),
        ],
    )

    out = tmp_path / "archive"
    result = ArchiveDeliverer().deliver([record], None, out)

    assert result.patient_count == 1
    patient_dirs = [p for p in (out / "patients").iterdir() if p.is_dir()]
    assert len(patient_dirs) == 1
    patient_dir = patient_dirs[0]
    pages = sorted((patient_dir / "encounters").glob("*.html"))
    assert len(pages) == 2, "two encounters must not collapse onto one page"

    # Every link on the patient page resolves to a file that exists.
    html = (patient_dir / "index.html").read_text(encoding="utf-8")
    links = re.findall(r'href="(encounters/[^"]+)"', html)
    assert sorted(links) == sorted(f"encounters/{p.name}" for p in pages)


def test_archive_same_name_patients_never_cross_attribute(tmp_path: Path) -> None:
    """The cross-leak failure mode: two distinct patients sharing both
    ``family_name`` and ``given_name`` (different ids, different DOBs) must
    each receive only their own PDFs. A deliverer that matched on the
    ``{family}_{given}_`` prefix would silently mix both.
    """
    from datetime import date

    from anastomosis.core.model import Encounter, Patient, PatientRecord

    # Two patients with the same family/given names — separated only by
    # patient.id (and DOB, which is informative but not consulted by the
    # archive's attribution path).
    def _make_record(patient_id: str, dos: date, enc_id: str) -> PatientRecord:
        return PatientRecord(
            patient=Patient(
                id=patient_id,
                family_name="Smith",
                given_name="John",
                birth_date=date(1980, 1, 1) if patient_id.endswith("1") else date(1995, 6, 15),
            ),
            encounters=[
                Encounter(id=enc_id, patient_id=patient_id, date_of_service=dos, note_type="SOAP")
            ],
        )

    rec_a = _make_record("aaaa-0000-0000-0000-000000000001", date(2023, 5, 10), "encA-0000-0000")
    rec_b = _make_record("bbbb-0000-0000-0000-000000000002", date(2024, 3, 15), "encB-0000-0000")

    pdfs_dir = tmp_path / "charts"
    pdfs_dir.mkdir(parents=True)
    # Two distinct PDFs, both named Smith_John_* (the cross-leak surface).
    pdf_a = pdfs_dir / "Smith_John_05-10-2023_SOAP.pdf"
    pdf_b = pdfs_dir / "Smith_John_03-15-2024_SOAP.pdf"
    pdf_a.write_bytes(b"%PDF-1.7 patient A\n")
    pdf_b.write_bytes(b"%PDF-1.7 patient B\n")

    # Write the render index so attribution is by patient_id, not prefix.
    RenderIndex.from_entries(
        [
            RenderEntry(pdf=pdf_a.name, patient_id=rec_a.patient.id, encounter_id="encA-0000-0000"),
            RenderEntry(pdf=pdf_b.name, patient_id=rec_b.patient.id, encounter_id="encB-0000-0000"),
        ]
    ).write(pdfs_dir)

    out = tmp_path / "archive"
    ArchiveDeliverer().deliver([rec_a, rec_b], pdfs_dir, out)

    # Each patient gets exactly its own PDF; never the other's.
    a_pdfs = sorted(p.name for p in (out / "patients" / rec_a.patient.id / "pdfs").glob("*.pdf"))
    b_pdfs = sorted(p.name for p in (out / "patients" / rec_b.patient.id / "pdfs").glob("*.pdf"))
    assert a_pdfs == [pdf_a.name], f"patient A got {a_pdfs}, expected only {pdf_a.name}"
    assert b_pdfs == [pdf_b.name], f"patient B got {b_pdfs}, expected only {pdf_b.name}"
    # And nothing leaked into ``unattributed/``.
    assert not (out / "unattributed").exists() or not list((out / "unattributed").glob("*.pdf")), (
        "no PDFs should be unattributed when every PDF is indexed"
    )


def test_archive_missing_render_index_routes_to_unattributed(tmp_path: Path) -> None:
    """When ``pdfs_dir`` has PDFs but no ``render_index.json``, the archive
    refuses to guess: every PDF lands in ``out/unattributed/`` and no patient
    directory receives any chart. The opposite of the silent cross-leak.
    """
    from datetime import date

    from anastomosis.core.model import Patient, PatientRecord

    record = PatientRecord(
        patient=Patient(
            id="cccc-0000-0000-0000-000000000003",
            family_name="Smith",
            given_name="John",
            birth_date=date(1980, 1, 1),
        ),
        encounters=[],
    )
    pdfs_dir = tmp_path / "charts"
    pdfs_dir.mkdir(parents=True)
    (pdfs_dir / "Smith_John_05-10-2023_SOAP.pdf").write_bytes(b"%PDF-1.7 unindexed\n")

    out = tmp_path / "archive"
    result = ArchiveDeliverer().deliver([record], pdfs_dir, out)

    # No PDF reached the patient.
    assert result.pdf_count == 0
    patient_pdfs = out / "patients" / record.patient.id / "pdfs"
    assert not patient_pdfs.exists() or not list(patient_pdfs.glob("*.pdf"))
    # The orphan landed in unattributed/.
    unattributed = sorted(p.name for p in (out / "unattributed").glob("*.pdf"))
    assert unattributed == ["Smith_John_05-10-2023_SOAP.pdf"]


def test_archive_unattributed_count_logs_only_successful_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: the unattributed-PDF sweep used to report ``len(orphans)``
    — the number of copies ATTEMPTED — while the two other chart-copy sites
    (attributed patient PDFs, the bundle deliverer) counted only successes.
    A copy that fails mid-sweep (disk full, permissions) must not inflate
    the "(N unattributed)" count past what actually landed on disk.
    """
    import logging

    import anastomosis.deliver._shared as shared
    from anastomosis.core.model import Patient, PatientRecord

    record = PatientRecord(patient=Patient(id="dddd-0000-0000-0000-000000000004"))
    pdfs_dir = tmp_path / "charts"
    pdfs_dir.mkdir()
    good = pdfs_dir / "aaa.pdf"
    bad = pdfs_dir / "bbb.pdf"
    good.write_bytes(b"%PDF-1.7 good\n")
    bad.write_bytes(b"%PDF-1.7 bad\n")
    # No render index at all -> both PDFs route through the unattributed sweep.

    real_copy = shared.copy_delivered_file

    def flaky_copy(source: Path, destination: Path) -> str | None:
        if source.name == bad.name:
            return "OSError"
        return real_copy(source, destination)

    # copy_claimed_chart (called by _route_unattributed_pdfs) looks up
    # copy_delivered_file as a module global at call time, so patching the
    # _shared module's attribute reaches it without touching archive.py.
    monkeypatch.setattr(shared, "copy_delivered_file", flaky_copy)

    out = tmp_path / "archive"
    with caplog.at_level(logging.INFO, logger="anastomosis.deliver.archive.archive"):
        ArchiveDeliverer().deliver([record], pdfs_dir, out)

    landed = sorted(p.name for p in (out / "unattributed").glob("*.pdf"))
    assert landed == [good.name], "the failed copy must never appear on disk"

    lines = [r.getMessage() for r in caplog.records if "archive delivered" in r.getMessage()]
    assert lines, "expected the archive-delivered summary log line"
    assert "1 unattributed" in lines[0], (
        f"logged unattributed count must match the {len(landed)} file(s) actually "
        f"copied, not the 2 attempted: {lines[0]!r}"
    )
    # And the one that did not copy is not simply forgotten: it was in the
    # charts folder and it is not in the archive, which is the run's missing
    # count wherever the copy failed (#225).
    assert "1 missing" in lines[0], lines[0]


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


def test_archive_missing_indexed_pdf_logs_surrogate_not_filename(
    tmp_path: Path, records: list[PatientRecord], caplog: pytest.LogCaptureFixture
) -> None:
    """When the render index names a PDF that is not on disk, the archive logs
    the WARNING by the patient's run-scoped surrogate — never the raw source
    GUID, and never the filename, which embeds the patient name and a
    MM-DD-YYYY date of service."""
    import logging

    record = records[0]
    assert record.encounters, "fixture record must expose at least one encounter"
    enc = record.encounters[0]
    pdfs_dir = tmp_path / "charts"
    pdfs_dir.mkdir()
    # An index entry naming a PDF that is never written to disk. The filename
    # embeds the (synthetic) patient name + a MM-DD-YYYY token on purpose — the
    # exact string that must NOT reach the log.
    missing = f"{record.patient.family_name}_{record.patient.given_name}_01-02-2020_SOAP.pdf"
    RenderIndex.from_entries(
        [RenderEntry(pdf=missing, patient_id=record.patient.id, encounter_id=enc.id)]
    ).write(pdfs_dir)

    out = tmp_path / "archive"
    with caplog.at_level(logging.WARNING, logger="anastomosis.deliver.archive.archive"):
        ArchiveDeliverer().deliver([record], pdfs_dir, out)

    hits = [r.getMessage() for r in caplog.records if "missing on disk" in r.getMessage()]
    assert hits, "a missing indexed PDF must be logged loudly"
    blob = "\n".join(hits)
    assert safe_log_id(record.patient.id) in blob, (
        "the run-scoped surrogate must identify the missing chart"
    )
    assert record.patient.id not in blob, "the raw source GUID must never reach the log"
    assert record.patient.family_name not in blob
    assert record.patient.given_name not in blob
    assert not re.search(r"\b\d{2}-\d{2}-\d{4}\b", blob), "a date-of-service token leaked"


def test_pipeline_never_logs_patient_names(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Ingest the synthetic PF/Tebra fixture and run the archive deliverer over
    it with a render index naming PDFs absent from disk (exercising the
    missing-on-disk warning), then assert no log line carries any fixture
    patient's family/given name. Names are collected from the ingested records,
    never hardcoded; no Chromium / rendering is required."""
    import logging

    with caplog.at_level(logging.DEBUG):
        loaded = list(get_source("pf-tebra").load(FIXTURE))
        pdfs_dir = tmp_path / "charts"
        pdfs_dir.mkdir()
        entries: list[RenderEntry] = []
        for rec in loaded:
            # One file per encounter, as the engine allocates them. A flat name
            # shared by all of a patient's encounters would be an index that
            # maps one chart onto several visits — which the index now refuses,
            # and which the engine cannot produce.
            for nth, enc in enumerate(rec.encounters):
                fname = (
                    f"{rec.patient.family_name}_{rec.patient.given_name}_01-02-2020_SOAP-{nth}.pdf"
                )
                entries.append(
                    RenderEntry(pdf=fname, patient_id=rec.patient.id, encounter_id=enc.id)
                )
        RenderIndex.from_entries(entries).write(pdfs_dir)
        ArchiveDeliverer().deliver(loaded, pdfs_dir, tmp_path / "archive")

    names: set[str] = set()
    for rec in loaded:
        for value in (rec.patient.family_name, rec.patient.given_name):
            if value:
                names.add(value)
    assert names, "fixture must expose patient names to guard against"
    blob = "\n".join(r.getMessage() for r in caplog.records)
    for name in names:
        assert name not in blob, f"patient name leaked into logs: {name!r}"
    # Nor may a raw source patient GUID appear — only its run-scoped surrogate.
    ids = {rec.patient.id for rec in loaded if rec.patient.id}
    assert ids, "fixture must expose patient ids to guard against"
    for pid in ids:
        assert pid not in blob, "a raw source patient GUID leaked into logs"


def test_archive_delivered_log_never_carries_output_path(
    tmp_path: Path, records: list[PatientRecord], caplog: pytest.LogCaptureFixture
) -> None:
    """The archive 'delivered' summary logs counts only — never the (operator-
    chosen, possibly patient-named) output PATH (SECURITY.md: never a path)."""
    import logging

    # A directory whose NAME stands in for a patient-derived operator dir.
    out = tmp_path / "Fixture_Ada_archive"
    with caplog.at_level(logging.DEBUG, logger="anastomosis.deliver.archive.archive"):
        ArchiveDeliverer().deliver(records, None, out)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    # No os.sep-joined output-dir string (and no bare dir name) reaches the log.
    assert str(out) not in blob
    assert out.name not in blob
    # The PHI-safe delivered-summary count line IS present.
    assert "archive delivered" in blob


def test_archive_budgets_the_copied_chart_and_keeps_the_link_true(tmp_path: Path) -> None:
    """A renderer-length chart name must still be DELIVERED, and the encounter
    page must link to the file that actually landed.

    The renderer names charts ``{family}_{given}_{dos}_{type}.pdf`` with every
    component capped at ``MAX_NAME_CHARS`` — up to ~617 characters. Copied into
    the archive unbudgeted, that raised an OSError the deliverer logged as "pdf
    copy failed" and continued past: a chart silently missing from the delivered
    tree. Budgeting the destination fixes it only if the page LINK is budgeted
    from the same call — otherwise the archive shows a link to nothing.
    """
    from datetime import date

    from anastomosis.core.model import Encounter, Patient, PatientRecord
    from anastomosis.core.textutil import MAX_PATH_CHARS

    pid = "feedface-0000-0000-0000-0000000000aa"
    enc_id = "feedface-e000-0000-0000-0000000000aa"
    record = PatientRecord(
        patient=Patient(id=pid, family_name="Fixture", given_name="Ada"),
        encounters=[Encounter(id=enc_id, patient_id=pid, date_of_service=date(2023, 5, 10))],
    )

    pdfs_dir = tmp_path / "charts"
    pdfs_dir.mkdir()
    # A renderer-shaped name at the length safe_name permits per component.
    chart = f"Fixture_Ada_05-10-2023_{'S' * 200}.pdf"
    (pdfs_dir / chart).write_bytes(b"%PDF-1.7 fake\n")
    RenderIndex.from_entries([RenderEntry(pdf=chart, patient_id=pid, encounter_id=enc_id)]).write(
        pdfs_dir
    )

    out = tmp_path / "archive"
    result = ArchiveDeliverer().deliver([record], pdfs_dir, out)

    # The chart was DELIVERED, not warned about and skipped.
    assert result.pdf_count == 1
    delivered = list((out / "patients" / pid / "pdfs").glob("*.pdf"))
    assert len(delivered) == 1
    assert len(str(delivered[0])) <= MAX_PATH_CHARS
    assert delivered[0].read_bytes() == b"%PDF-1.7 fake\n"

    # ...and the encounter page's link resolves to exactly that file.
    page = next((out / "patients" / pid / "encounters").glob("*.html"))
    hrefs = re.findall(r'href="\.\./pdfs/([^"]+)"', page.read_text(encoding="utf-8"))
    assert hrefs == [delivered[0].name]


def test_archive_refuses_a_chart_it_cannot_name(tmp_path: Path) -> None:
    """An output tree too deep to hold ANY distinct chart name fails loudly.

    This is the ``unattributed/`` route (no render index, so nothing is
    guessed onto a patient) with an operator-chosen directory deep enough that
    not even a hash tag fits underneath it. Warn-and-continue here would hand
    over a tree missing a chart; the budget raises instead.
    """
    from anastomosis.core.textutil import MAX_PATH_CHARS

    # Deep enough that ``<out>/unattributed/<name>.pdf`` has no room left.
    padding = MAX_PATH_CHARS - 30 - len(str(tmp_path)) - len("/archive")
    out = tmp_path / ("d" * padding) / "archive"

    pdfs_dir = tmp_path / "charts"
    pdfs_dir.mkdir()
    (pdfs_dir / "Fixture_Ada_05-10-2023_SOAP.pdf").write_bytes(b"%PDF-1.7 fake\n")

    with pytest.raises(ValueError, match="path budget"):
        ArchiveDeliverer().deliver([], pdfs_dir, out)


def test_archive_refuses_two_patient_ids_that_sanitize_alike(tmp_path: Path) -> None:
    """``MRN 1234`` and ``MRN/1234`` both sanitize to ``MRN_1234``.

    Every writer under ``patients/<id>/`` is exist_ok/overwrite, so a silent
    collision would file the second patient's bundle, pages, and charts into
    the first patient's directory — a merged chart, the worst outcome the
    toolkit has. The run must stop instead.
    """
    from anastomosis.core.model import Patient, PatientRecord
    from anastomosis.deliver._shared import DeliveredNameCollision

    records = [
        PatientRecord(patient=Patient(id="MRN 1234", family_name="Fixture", given_name="Ada")),
        PatientRecord(patient=Patient(id="MRN/1234", family_name="Sample", given_name="Boris")),
    ]

    with pytest.raises(DeliveredNameCollision, match="patient directory"):
        ArchiveDeliverer().deliver(records, None, tmp_path / "archive")


def test_archive_two_patient_ids_past_truncation_get_distinct_dirs(tmp_path: Path) -> None:
    """Two patient ids sharing every character up to the truncation cut,
    differing only in the tail that gets cut away, must not collapse onto one
    ``patients/<id>/`` slot. ``budgeted_name`` hashes the FULL original value
    (not the kept prefix), so the two land on distinct hash-tagged names —
    the ``_shared`` discipline's expected path, confirmed rather than assumed.
    """
    from anastomosis.core.model import Patient, PatientRecord

    base = "feedface-0000-0000-0000-0000000000aa" + "z" * 300
    records = [
        PatientRecord(patient=Patient(id=base + "-one", family_name="Fixture", given_name="Ada")),
        PatientRecord(patient=Patient(id=base + "-two", family_name="Sample", given_name="Bo")),
    ]

    out = tmp_path / "archive"
    result = ArchiveDeliverer().deliver(records, None, out)

    assert result.patient_count == 2
    patient_dirs = [p for p in (out / "patients").iterdir() if p.is_dir()]
    assert len(patient_dirs) == 2, "two ids differing only past the cut must not collapse"


def test_archive_two_encounter_ids_that_sanitize_alike_raise_without_patient_values(
    tmp_path: Path,
) -> None:
    """Two encounters within ONE patient whose ids differ only in characters
    ``safe_name`` strips (``enc 0001`` and ``enc/0001`` both become
    ``enc_0001``) must not silently overwrite one encounter's page with the
    other's — the same discipline the patient-directory ledger already
    enforces, now applied to encounter pages (``encounters/`` writes are
    ``exist_ok``, so without a claim this merges the two into one file). The
    refusal must carry no patient- or encounter-derived value.
    """
    from datetime import date

    from anastomosis.core.model import Encounter, Patient, PatientRecord
    from anastomosis.deliver._shared import DeliveredNameCollision

    pid = "feedface-0000-0000-0000-0000000000aa"
    record = PatientRecord(
        patient=Patient(id=pid, family_name="Fixture", given_name="Ada"),
        encounters=[
            Encounter(id="enc 0001", patient_id=pid, date_of_service=date(2023, 5, 10)),
            Encounter(id="enc/0001", patient_id=pid, date_of_service=date(2023, 6, 10)),
        ],
    )

    with pytest.raises(DeliveredNameCollision, match="encounter page") as excinfo:
        ArchiveDeliverer().deliver([record], None, tmp_path / "archive")

    message = str(excinfo.value)
    assert "enc 0001" not in message
    assert "enc/0001" not in message
    assert "enc_0001" not in message
    assert pid not in message
    assert "Fixture" not in message
    assert "Ada" not in message


def test_archive_budgeted_chart_is_not_also_routed_to_unattributed(tmp_path: Path) -> None:
    """A chart delivered under a BUDGETED name is still 'owned'.

    Ownership is tracked by the SOURCE filename, because the unattributed
    sweep looks at ``pdfs_dir`` where the renderer's own long name still
    stands. Matching against the shortened DELIVERED name instead would leave
    the source unclaimed and copy the chart a second time into
    ``unattributed/`` — the same chart in two places, one of them labelled
    "we could not attribute this".
    """
    from datetime import date

    from anastomosis.core.model import Encounter, Patient, PatientRecord

    pid = "feedface-0000-0000-0000-0000000000aa"
    enc_id = "feedface-e000-0000-0000-0000000000aa"
    record = PatientRecord(
        patient=Patient(id=pid, family_name="Fixture", given_name="Ada"),
        encounters=[Encounter(id=enc_id, patient_id=pid, date_of_service=date(2023, 5, 10))],
    )

    pdfs_dir = tmp_path / "charts"
    pdfs_dir.mkdir()
    chart = f"Fixture_Ada_05-10-2023_{'S' * 200}.pdf"
    (pdfs_dir / chart).write_bytes(b"%PDF-1.7 fake\n")
    RenderIndex.from_entries([RenderEntry(pdf=chart, patient_id=pid, encounter_id=enc_id)]).write(
        pdfs_dir
    )

    out = tmp_path / "archive"
    ArchiveDeliverer().deliver([record], pdfs_dir, out)

    assert not (out / "unattributed").exists(), "an attributed chart must not be duplicated"
    assert len(list((out / "patients" / pid / "pdfs").glob("*.pdf"))) == 1


# --- the source's own documents reach the patient (#110) ---------------------


def _charts_with_attachments(tmp_path: Path, records: list[PatientRecord]) -> Path:
    """A charts directory holding the attachments a run would have carried."""
    from anastomosis.pipeline import ATTACHMENTS_DIRNAME

    charts = tmp_path / "charts"
    landing = charts / ATTACHMENTS_DIRNAME
    landing.mkdir(parents=True)
    for record in records:
        for doc in record.documents:
            if doc.path:
                (landing / Path(doc.path).name).write_bytes((FIXTURE / doc.path).read_bytes())
    return charts


def test_a_patients_documents_land_in_their_own_directory(
    tmp_path: Path, records: list[PatientRecord]
) -> None:
    """The scans and lab reports a chart references, delivered beside it.

    Attribution needs no index: a chart's filename carries no patient id, so
    ownership had to be looked up, but a document is named BY the record that
    owns it. What this pins is that nothing lands in a directory whose record
    did not ask for it.
    """
    charts = _charts_with_attachments(tmp_path, records)
    expected = {Path(d.path).name for r in records for d in r.documents if d.path}
    assert expected, "the fixture no longer exercises this path"

    result = ArchiveDeliverer().deliver(records, charts, tmp_path / "archive")

    patients = tmp_path / "archive" / "patients"
    delivered = {p.name for p in patients.rglob("attachments/*") if p.suffix}
    assert delivered == expected
    assert result.attachment_count == len(expected)

    # And each one sits under a patient whose own record names it — a document
    # filed into the wrong patient's folder is the failure this tool exists to
    # prevent, and it would still satisfy the set comparison above.
    by_patient_dir = {
        page.parent.name: {
            Path(d.path).name
            for r in records
            for d in r.documents
            if d.path and r.patient.id in page.read_text(encoding="utf-8")
        }
        for page in patients.rglob("index.html")
    }
    for directory, owned in by_patient_dir.items():
        here = {p.name for p in (patients / directory / "attachments").glob("*") if p.suffix}
        assert here <= owned, f"{directory} holds a document its record does not name"


def test_the_patient_page_links_each_document_by_name_and_length(
    tmp_path: Path, records: list[PatientRecord]
) -> None:
    """A delivered file nobody links to is half a delivery."""
    charts = _charts_with_attachments(tmp_path, records)

    ArchiveDeliverer().deliver(records, charts, tmp_path / "archive")

    pages = [
        page.read_text(encoding="utf-8")
        for page in (tmp_path / "archive" / "patients").rglob("index.html")
    ]
    with_docs = [page for page in pages if "<h3>Documents (" in page]
    assert with_docs, "no patient page lists the documents delivered beside it"
    assert 'href="attachments/' in with_docs[0]
    assert "Cardiology referral letter" in with_docs[0], "the title, not the storage id"
    assert "2 pages" in with_docs[0], "the page count a chart is incomplete without"


def test_a_document_missing_from_the_charts_dir_is_named_not_counted(
    tmp_path: Path, records: list[PatientRecord], caplog: pytest.LogCaptureFixture
) -> None:
    """Conservation belongs to the run; the deliverer must not overstate.

    `pipeline._carry_attachments` knows the export and stops a run whose
    attachments did not all arrive. This deliverer only sees a charts
    directory, so a record naming a document that is not in it means the
    directory was assembled without the carry step or edited after — it says
    so, by surrogate id, and does not count what it did not deliver.
    """
    charts = tmp_path / "charts"
    charts.mkdir()  # no attachments/ at all

    with caplog.at_level(logging.WARNING):
        result = ArchiveDeliverer().deliver(records, charts, tmp_path / "archive")

    assert result.attachment_count == 0
    assert any("not in the charts directory" in r.message for r in caplog.records)
    logged = " ".join(r.getMessage() for r in caplog.records)
    for record in records:
        assert record.patient.id not in logged, "a raw patient id reached the log"
        for doc in record.documents:
            assert not doc.path or Path(doc.path).name not in logged, (
                "an attachment filename — a source identifier — reached the log"
            )
