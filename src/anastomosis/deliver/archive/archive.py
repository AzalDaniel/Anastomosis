"""Offline archive deliverer.

Produces a static, browsable tree from canonical PatientRecords: one
``index.html`` (search + inline-JSON manifest), one ``patients/<id>/``
subtree per patient (HTML summary, FHIR R4 Bundle JSON, rendered chart
PDFs), and one ``assets/`` directory. Zero network, strict CSP, id-based
folder naming, no inline JS (RULES.md 38-40); hardened via
``secure_output_dir`` (RULES.md 18).

``index.json`` entry: ``{id, display_name, dob, encounter_count, search}``."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from anastomosis.core.atomic import atomic_copy, atomic_write_text
from anastomosis.core.clock import now as _clock_now
from anastomosis.core.conservation import Conservation
from anastomosis.core.fhir import DeliveredAttachment
from anastomosis.core.logutil import safe_log_id
from anastomosis.core.model import Encounter, PatientRecord
from anastomosis.core.output import secure_output_dir
from anastomosis.core.textutil import HASH_TAG_CHARS, budgeted_name
from anastomosis.deliver._shared import (
    claim_delivered_name,
    copy_claimed_chart,
    measured_attachment,
    record_witness,
    write_fhir_bundle,
)
from anastomosis.deliver.render_index import RenderIndex
from anastomosis.pipeline import ATTACHMENTS_DIRNAME
from anastomosis.qa import QAReport

from .templates import CSP_META_CONTENT, ENCOUNTER_HTML, INDEX_HTML, PATIENT_HTML, README_TEXT
from .templates import build_env as _build_env

__all__ = ["ArchiveDeliverer", "ArchiveResult"]

logger = logging.getLogger(__name__)

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
# Files copied into out_dir/assets/ on every run. Anything else in the source
# assets directory is documentation and stays inside the package.
_ASSET_FILES: tuple[str, ...] = ("anast.css", "anast-index.js")
# Reserve = the longest fixed wrapper (``/encounters/`` + ``.html``) plus a
# budgeted name's shortest distinct form (its hash tag) — not a guess at a
# plausible child name; every child is itself budgeted separately.
_PATIENT_CHILD_RESERVE = len("/encounters/") + HASH_TAG_CHARS + len(".html")


def _date_iso(value: object) -> str | None:
    """Render a date/datetime as ISO-8601, or None for missing values."""
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        result = iso()
        return result if isinstance(result, str) else None
    return str(value)


@dataclass(frozen=True)
class _PatientCharts:
    """What copying one patient's charts produced — including what it did not.

    ``missing`` is the point of the type: every field the caller needs to
    count losses without re-deriving them from ``by_encounter``/``claimed_sources``."""

    #: encounter id -> the DELIVERED filename its chart was written under.
    by_encounter: dict[str, str]
    #: Source filenames this patient claimed — what the unattributed sweep
    #: matches against (pre-budget names, from the charts directory).
    claimed_sources: set[str]
    #: Encounter ids with no chart in this patient's folder; drives the
    #: per-encounter page's missing-chart link.
    missing: set[str]
    #: Charts missing from the directory entirely (not just failed-copy);
    #: kept separate so a failed copy isn't double-counted in the sweep.
    absent: int


@dataclass(frozen=True)
class ArchiveResult:
    """What landed on disk, summarized for the CLI."""

    out_dir: Path
    patient_count: int
    encounter_count: int
    pdf_count: int
    #: Source attachments delivered (scans, lab reports) — separate from
    #: `pdf_count` so either going missing is never hidden by the other.
    attachment_count: int
    index_path: Path
    #: Charts the index named that never landed (missing file or failed
    #: copy) — the difference between "two visits" and "a lost chart".
    missing_count: int = 0
    #: Charts filed under ``unattributed/`` rather than guessed onto a
    #: patient — not a loss, but nobody opens that folder unasked.
    unattributed_count: int = 0


def _chart_conservation(
    render_index: RenderIndex | None,
    records: list[PatientRecord],
    pdfs_dir: Path | None,
    *,
    delivered: int,
    unattributed: int,
    missing: int,
) -> Conservation:
    """The canonical -> delivered seam every chart this run answers for.

    Offered = index-named charts unioned with every PDF on disk (an
    earlier run's leftovers still count). Dispositions: delivered,
    unattributed, or missing — no fourth (#110)."""
    named: set[str] = set()
    if render_index is not None:
        for record in records:
            named.update(render_index.for_patient(record.patient.id))
    on_disk: set[str] = set()
    if pdfs_dir is not None and pdfs_dir.is_dir():
        on_disk = {path.name for path in pdfs_dir.glob("*.pdf")}
    return Conservation(
        stage="canonical -> delivered",
        unit="chart",
        offered=len(named | on_disk),
        dispositions={
            "delivered": delivered,
            "unattributed": unattributed,
            "missing": missing,
        },
    )


class ArchiveDeliverer:
    """Render canonical records as a static, offline-readable archive."""

    def __init__(self, generator: str | None = None) -> None:
        import anastomosis

        self.generator = generator or f"anastomosis {anastomosis.__version__}"
        self._env = _build_env()
        self._index_template = self._env.from_string(INDEX_HTML)
        self._patient_template = self._env.from_string(PATIENT_HTML)
        self._encounter_template = self._env.from_string(ENCOUNTER_HTML)

    # --- public entry point -------------------------------------------------

    def deliver(
        self,
        records: Iterable[PatientRecord],
        pdfs_dir: Path | None,
        out_dir: str | Path,
        *,
        qa_report: QAReport | None = None,
    ) -> ArchiveResult:
        out = secure_output_dir(out_dir)
        self._copy_assets(out)
        render_index = RenderIndex.load(pdfs_dir)

        manifest_entries: list[dict[str, object]] = []
        encounter_count = 0
        pdf_count = 0
        attachment_count = 0
        missing_count = 0
        generated_at = _clock_now().isoformat()

        records_list = list(records)
        qa_lookup = _qa_lookup(qa_report)
        owned_pdfs: set[str] = set()
        # Per-run ledger: two ids that sanitize to one name, or two records
        # under one id, would otherwise merge into one exist_ok slot. A
        # second claimant is a hard failure.
        claimed_dirs: dict[str, str] = {}

        for record in records_list:
            # Budgeted against the tree being built, so a long source id can
            # never turn a delivered chart into a mid-archive FileNotFoundError.
            pid = budgeted_name(
                record.patient.id,
                "unknown",
                parent=out / "patients",
                reserve=_PATIENT_CHILD_RESERVE,
            )
            # The record is the witness: a patient id is not guaranteed unique,
            # so two records under one id would otherwise merge silently here.
            claim_delivered_name(
                claimed_dirs,
                pid,
                record.patient.id,
                kind="patient directory",
                content=record_witness(record),
            )
            patient_dir = out / "patients" / pid
            (patient_dir / "encounters").mkdir(parents=True, exist_ok=True)

            # Copied before the FHIR bundle: the record alone can't say what
            # name a document lands under, so the bundle needs what this measured.
            patient_attachments, landed_attachments = self._copy_patient_attachments(
                record, (pdfs_dir or out) / ATTACHMENTS_DIRNAME, patient_dir
            )
            attachment_count += len(patient_attachments)

            # Carries what was just measured above, so every DocumentReference
            # resolves to a real file beside it.
            write_fhir_bundle(record, patient_dir, landed_attachments)

            # Attributed strictly via the render index (RULES.md 11), never a
            # name-prefix guess. Ownership tracks the SOURCE name (what the
            # unattributed sweep sees in ``pdfs_dir``), not the budgeted
            # delivered name, or an already-filed chart would re-copy there too.
            charts = self._copy_patient_pdfs(record, render_index, pdfs_dir, patient_dir)
            patient_pdfs = charts.by_encounter
            owned_pdfs.update(charts.claimed_sources)
            pdf_count += len(patient_pdfs)
            missing_count += charts.absent

            # Ledger is fresh per patient: page names only need to be distinct
            # within this patient's own encounters/ directory.
            encounter_count += len(record.encounters)
            claimed_pages: dict[str, str] = {}
            for encounter in record.encounters:
                self._write_encounter_page(
                    encounter,
                    record,
                    patient_dir,
                    patient_pdfs,
                    qa_lookup,
                    generated_at,
                    claimed_pages,
                    chart_missing=encounter.id in charts.missing,
                )

            self._write_patient_page(record, patient_dir, generated_at, patient_attachments)

            manifest_entries.append(_manifest_entry(record, pid))

        # Anything in ``pdfs_dir`` not claimed by an indexed patient lands
        # in ``unattributed/`` so nothing is silently dropped or guessed.
        unattributed_count, sweep_failures = self._route_unattributed_pdfs(
            pdfs_dir, render_index, owned_pdfs, out
        )
        # A failed attributed copy leaves a chart unclaimed for the sweep to
        # settle; counting it above too would double-count one chart.
        missing_count += sweep_failures

        _chart_conservation(
            render_index,
            records_list,
            pdfs_dir,
            delivered=pdf_count,
            unattributed=unattributed_count,
            missing=missing_count,
        ).check()

        index_path = self._write_index(
            out,
            manifest_entries,
            encounter_count=encounter_count,
            generated_at=generated_at,
        )
        self._write_readme(out)
        logger.info(
            "archive delivered: %d patients, %d encounters, %d pdfs, "
            "%d attachments (%d missing, %d unattributed)",
            len(manifest_entries),
            encounter_count,
            pdf_count,
            attachment_count,
            missing_count,
            unattributed_count,
        )
        return ArchiveResult(
            out_dir=out,
            patient_count=len(manifest_entries),
            encounter_count=encounter_count,
            pdf_count=pdf_count,
            attachment_count=attachment_count,
            index_path=index_path,
            missing_count=missing_count,
            unattributed_count=unattributed_count,
        )

    # --- writers ------------------------------------------------------------

    def _copy_assets(self, out: Path) -> None:
        assets_dir = out / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        for name in _ASSET_FILES:
            source = _ASSETS_DIR / name
            if not source.is_file():
                # Loud failure — a missing asset is a packaging bug, not a
                # silent fallback. The archive must be self-contained.
                raise FileNotFoundError(f"archive asset missing from package: {name}")
            atomic_copy(source, assets_dir / name)
        notice = _ASSETS_DIR / "NOTICE.txt"
        if notice.is_file():
            licenses_dir = out / "LICENSES"
            licenses_dir.mkdir(parents=True, exist_ok=True)
            atomic_copy(notice, licenses_dir / "NOTICE.txt")

    def _copy_patient_attachments(
        self,
        record: PatientRecord,
        attachments_dir: Path,
        patient_dir: Path,
    ) -> tuple[list[dict[str, object]], dict[str, DeliveredAttachment]]:
        """Copy this patient's source attachments; measure each for the FHIR
        rendition beside it.

        Returns the patient-page list and a FHIR-bundle dict keyed by
        `DocumentArtifact` id (two artifacts may name one file, no index needed)."""
        wanted = [doc for doc in record.documents if doc.path]
        if not wanted:
            return [], {}

        out_dir = patient_dir / ATTACHMENTS_DIRNAME
        out_dir.mkdir(parents=True, exist_ok=True)
        claims: dict[str, str] = {}
        delivered: list[dict[str, object]] = []
        landed: dict[str, DeliveredAttachment] = {}  # source filename -> what was measured
        by_doc: dict[str, DeliveredAttachment] = {}
        for doc in wanted:
            name = Path(doc.path or "").name
            source = attachments_dir / name
            if not source.is_file():
                # Reaching here means the charts directory was edited after
                # the run (pipeline refuses this case outright); logs the
                # surrogate id, never the filename.
                logger.warning(
                    "record names an attachment missing from the charts directory for patient %s",
                    safe_log_id(record.patient.id),
                )
                continue
            copied, failure = copy_claimed_chart(out_dir, claims, source, name, kind="attachment")
            if failure or copied is None:
                logger.warning(
                    "attachment could not be delivered for patient %s (%s)",
                    safe_log_id(record.patient.id),
                    failure,
                )
                continue
            delivered.append(
                {"name": copied, "title": doc.title or copied, "pages": doc.page_count}
            )
            # Two artifacts naming ONE source file measure it once
            # (`measured_attachment`), so both resolve to the file that exists.
            by_doc[doc.id] = measured_attachment(
                landed, out_dir / copied, f"{ATTACHMENTS_DIRNAME}/{copied}"
            )

        missing = len(wanted) - len(delivered)
        if missing:
            # A warning, not a refusal: conservation belongs to the run
            # (`pipeline._carry_attachments` already stops it if an attachment
            # never arrived). Reaching here means the charts directory was
            # assembled or edited outside that step.
            logger.warning(
                "%d attachment(s) named by a record are not in the charts directory for patient %s",
                missing,
                safe_log_id(record.patient.id),
            )
        return delivered, by_doc

    def _copy_patient_pdfs(
        self,
        record: PatientRecord,
        render_index: RenderIndex | None,
        pdfs_dir: Path | None,
        patient_dir: Path,
    ) -> _PatientCharts:
        """Copy this patient's PDFs into ``pdfs/`` (RULES.md 11); no index
        entries means no PDFs.

        Destination naming (:func:`copy_claimed_chart`) is a hard failure; a
        chart the index names but never arrives is COUNTED, not just logged."""
        empty = _PatientCharts({}, set(), set(), 0)
        if render_index is None or pdfs_dir is None or not pdfs_dir.is_dir():
            return empty
        names = render_index.for_patient(record.patient.id)
        if not names:
            return empty

        out_dir = patient_dir / "pdfs"
        out_dir.mkdir(parents=True, exist_ok=True)
        mapping: dict[str, str] = {}
        claimed: dict[str, str] = {}
        claimed_sources: set[str] = set()
        missing: set[str] = set()
        absent = 0

        def lost(name: str) -> None:
            """Remember WHICH encounter lost its chart, not only how many did.

            The per-encounter page needs the id: it can then say the chart is
            missing instead of quietly rendering without the link.
            """
            entry = render_index.lookup(name)
            if entry is not None:
                missing.add(entry.encounter_id)

        for name in names:
            source = pdfs_dir / name
            if not source.is_file():
                # The index claims a PDF the engine never wrote, or it was
                # deleted post-render; log the surrogate id only, never fake it.
                logger.warning(
                    "indexed pdf missing on disk for patient %s", safe_log_id(record.patient.id)
                )
                lost(name)
                absent += 1
                continue
            # Naming (not I/O) raises outside the warn path: an unnameable
            # destination fails loud rather than leaving the chart out silently.
            delivered, failure = copy_claimed_chart(out_dir, claimed, source, name, kind="chart")
            if failure is not None:
                logger.warning("pdf copy failed (%s)", failure)
                lost(name)
                continue
            assert delivered is not None  # copy_claimed_chart: failure is None => delivered isn't
            claimed_sources.add(name)
            entry = render_index.lookup(name)
            if entry is not None:
                # First-wins: a doubled encounter→pdf row (corrupted index)
                # keeps the first assignment, never overwrites.
                mapping.setdefault(entry.encounter_id, delivered)
        return _PatientCharts(mapping, claimed_sources, missing, absent)

    def _route_unattributed_pdfs(
        self,
        pdfs_dir: Path | None,
        render_index: RenderIndex | None,
        owned: set[str],
        out: Path,
    ) -> tuple[int, int]:
        """Copy leftover PDFs into ``out/unattributed/`` (fail-closed): PDFs
        the index does not mention, or the whole directory with no index.

        Returns ``(copied, failed)`` — a copy that failed here is missing,
        not "unattributed": it belongs to the run's missing count."""
        if pdfs_dir is None or not pdfs_dir.is_dir():
            return 0, 0
        all_pdfs = sorted(p for p in pdfs_dir.glob("*.pdf"))
        if not all_pdfs:
            return 0, 0
        if render_index is None:
            # No index at all: every PDF is unattributed by the same
            # fail-closed rule (count only — the path stays out of logs).
            logger.warning(
                "no render index; routing all %d pdf(s) to unattributed/",
                len(all_pdfs),
            )
            orphans = all_pdfs
        else:
            orphans = [p for p in all_pdfs if p.name not in owned]
        if not orphans:
            return 0, 0
        target = out / "unattributed"
        target.mkdir(parents=True, exist_ok=True)
        claimed: dict[str, str] = {}
        copied = 0
        failed = 0
        for source in orphans:
            # Budgeted and claimed exactly like an attributed chart: a PDF that
            # lands here is still a chart nobody may lose.
            _delivered, failure = copy_claimed_chart(
                target, claimed, source, source.name, kind="unattributed chart"
            )
            if failure is not None:
                logger.warning("unattributed pdf copy failed (%s)", failure)
                failed += 1
                continue
            copied += 1
        return copied, failed

    def _write_patient_page(
        self,
        record: PatientRecord,
        patient_dir: Path,
        generated_at: str,
        attachments: list[dict[str, object]] | None = None,
    ) -> None:
        encounters_ctx = [
            {
                "safe_id": _encounter_page_id(patient_dir, enc),
                "label": _encounter_label(enc),
                "chief_complaint": enc.chief_complaint,
            }
            for enc in record.encounters
        ]
        html = self._patient_template.render(
            csp=CSP_META_CONTENT,
            asset_prefix="../../",
            display_name=record.patient.display_name or "Unknown",
            dob=_date_iso(record.patient.birth_date),
            sex=record.patient.sex,
            patient_id=record.patient.id,
            identifiers=[
                {"kind": ident.kind.value, "value": ident.value}
                for ident in record.patient.identifiers
            ],
            encounters=encounters_ctx,
            attachments=attachments or [],
            conditions=[c.display for c in record.conditions if c.display],
            allergies=[a.substance for a in record.allergies if a.substance],
            medications=[m.display_name for m in record.medications if m.display_name],
            generator=self.generator,
            generated_at=generated_at,
        )
        atomic_write_text(patient_dir / "index.html", html)

    def _write_encounter_page(
        self,
        encounter: Encounter,
        record: PatientRecord,
        patient_dir: Path,
        patient_pdfs: dict[str, str],
        qa_lookup: dict[str, str],
        generated_at: str,
        claimed_pages: dict[str, str],
        *,
        chart_missing: bool = False,
    ) -> None:
        sections_ctx = [
            {"kind": s.kind.value, "title": s.title, "text": (s.text or "").strip()}
            for s in encounter.sections
        ]
        addenda_ctx = [
            {
                "text": (a.text or "").strip(),
                "author": a.author_name,
                "at": _date_iso(a.at),
            }
            for a in encounter.addenda
        ]
        html = self._encounter_template.render(
            csp=CSP_META_CONTENT,
            asset_prefix="../../../",
            label=_encounter_label(encounter),
            display_name=record.patient.display_name or "Unknown",
            date_of_service=_date_iso(encounter.date_of_service),
            chief_complaint=encounter.chief_complaint,
            note_type=encounter.note_type,
            pdf_name=patient_pdfs.get(encounter.id),
            # Two different reasons for no chart link: never rendered (no
            # loss) vs. rendered-but-missing (a loss, gets a sentence).
            chart_missing=chart_missing,
            qa_verdict=qa_lookup.get(encounter.id),
            sections=sections_ctx,
            addenda=addenda_ctx,
            generator=self.generator,
            generated_at=generated_at,
        )
        page_id = _encounter_page_id(patient_dir, encounter)
        # The rendered page is the claim's witness: encounter ids are not
        # guaranteed unique (a C-CDA may repeat one <id root>), so without it
        # a second visit would silently replace the first's page.
        claim_delivered_name(
            claimed_pages, page_id, encounter.id, kind="encounter page", content=html
        )
        encounter_file = patient_dir / "encounters" / f"{page_id}.html"
        atomic_write_text(encounter_file, html)

    def _write_index(
        self,
        out: Path,
        manifest_entries: list[dict[str, object]],
        *,
        encounter_count: int,
        generated_at: str,
    ) -> Path:
        # INVARIANT: no record value can terminate the inline <script> block.
        # ``json.dumps`` doesn't escape ``<``; escaping every ``</`` (JSON-
        # equivalent) stops a chart title from closing the tag early.
        index_json = json.dumps(manifest_entries, sort_keys=True).replace("</", "<\\/")
        html = self._index_template.render(
            csp=CSP_META_CONTENT,
            asset_prefix="",
            title="Anastomosis archive",
            generator=self.generator,
            generated_at=generated_at,
            patient_count=len(manifest_entries),
            encounter_count=encounter_count,
            index_json=index_json,
        )
        index_path = out / "index.html"
        # PHI-BY-DESIGN: the manifest names patients for the search box;
        # output dir already hardened (RULES.md 18). See SECURITY.md.
        # codeql[py/clear-text-storage-sensitive-data]
        atomic_write_text(index_path, html)
        # PHI-BY-DESIGN: same hardened directory as the index above.
        # codeql[py/clear-text-storage-sensitive-data]
        atomic_write_text(
            out / "index.json", json.dumps(manifest_entries, indent=2, sort_keys=True)
        )
        return index_path

    def _write_readme(self, out: Path) -> None:
        readme = out / "README.txt"
        atomic_write_text(readme, README_TEXT)


# --- helpers ----------------------------------------------------------------


def _encounter_page_id(patient_dir: Path, encounter: Encounter) -> str:
    """The encounter page's filename stem, budgeted for its full path.

    One definition, called by both the linker and the writer; a second,
    differently-budgeted derivation would produce a link pointing at
    nothing the operator can reach."""
    return budgeted_name(
        encounter.id, "encounter", parent=patient_dir / "encounters", suffix=".html"
    )


def _qa_lookup(qa_report: QAReport | None) -> dict[str, str]:
    if qa_report is None:
        return {}
    out: dict[str, str] = {}
    for doc in qa_report.documents:
        out[doc.encounter_id] = doc.verdict.value
    return out


def _encounter_label(encounter: Encounter) -> str:
    parts: list[str] = []
    if encounter.date_of_service is not None:
        parts.append(encounter.date_of_service.isoformat())
    if encounter.note_type:
        parts.append(encounter.note_type)
    elif encounter.encounter_type:
        parts.append(encounter.encounter_type)
    return " — ".join(parts) if parts else encounter.id


def _manifest_entry(record: PatientRecord, safe_id: str) -> dict[str, object]:
    """Searchable manifest row — see :mod:`archive` docstring for the schema."""
    patient = record.patient
    chief_complaints = [enc.chief_complaint for enc in record.encounters if enc.chief_complaint]
    note_shadows: list[str] = []
    for encounter in record.encounters:
        for section in encounter.sections:
            if section.text:
                note_shadows.append(section.text)
    haystack_parts: list[str] = []
    if patient.display_name:
        haystack_parts.append(patient.display_name)
    dob_iso = _date_iso(patient.birth_date)
    if dob_iso:
        haystack_parts.append(dob_iso)
    haystack_parts.extend(chief_complaints)
    haystack_parts.extend(note_shadows)
    haystack = " ".join(haystack_parts).lower()
    # Keep the searchable haystack bounded — long note bodies would otherwise
    # dominate the inline JSON without changing the search-quality story.
    return {
        "id": safe_id,
        "display_name": patient.display_name or patient.id,
        "dob": dob_iso,
        "encounter_count": len(record.encounters),
        "search": haystack[:4000],
    }
