"""Offline archive deliverer, in either grouping.

``Grouping.ARCHIVE`` writes one cross-patient tree: ``index.html`` (search
+ inline-JSON manifest), ``patients/<id>/`` (HTML summary, FHIR R4 Bundle
JSON, chart PDFs), ``assets/``. ``Grouping.BUNDLE`` writes one
self-contained ``<id>/`` per patient (bundle, charts, QA slice, README) and
no cross-patient navigation. Zero network, strict CSP, id-based folder
naming, no inline JS (RULES.md 38-40); hardened via ``secure_output_dir``.

``index.json`` entry: ``{id, display_name, dob, encounter_count, search}``."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
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
    copy_claimed_charts,
    measured_attachment,
    record_witness,
    write_fhir_bundle,
)
from anastomosis.deliver.render_index import RenderIndex
from anastomosis.pipeline import ATTACHMENTS_DIRNAME
from anastomosis.qa import DocumentQA, QAReport, Verdict

from .templates import (
    BUNDLE_README_TEXT,
    CSP_META_CONTENT,
    ENCOUNTER_HTML,
    INDEX_HTML,
    PATIENT_HTML,
    README_TEXT,
)
from .templates import build_env as _build_env

__all__ = ["ArchiveDeliverer", "ArchiveResult", "BundleResult", "Grouping"]

logger = logging.getLogger(__name__)

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
# Files copied into out_dir/assets/ on every run. Anything else in the source
# assets directory is documentation and stays inside the package.
_ASSET_FILES: tuple[str, ...] = ("anast.css", "anast-index.js")
# Reserve = the longest fixed wrapper plus a budgeted name's shortest distinct
# form (its hash tag) — not a guess at a plausible child name; every child is
# itself budgeted separately.
_ARCHIVE_CHILD_RESERVE = len("/encounters/") + HASH_TAG_CHARS + len(".html")
_BUNDLE_CHILD_RESERVE = len("/pdfs/") + HASH_TAG_CHARS + len(".pdf")


class Grouping(Enum):
    """How delivered patients sit in the output tree."""

    #: One cross-patient tree under a search index.
    ARCHIVE = "archive"
    #: One self-contained directory per patient, no cross-patient navigation.
    BUNDLE = "bundle"


@dataclass(frozen=True)
class _Layout:
    """Where one grouping puts a patient directory, and what it budgets for."""

    #: Path segments between the output root and a patient directory.
    parent: tuple[str, ...]
    #: Room the patient directory name leaves for its own deepest child.
    reserve: int


_LAYOUTS: dict[Grouping, _Layout] = {
    Grouping.ARCHIVE: _Layout(("patients",), _ARCHIVE_CHILD_RESERVE),
    Grouping.BUNDLE: _Layout((), _BUNDLE_CHILD_RESERVE),
}


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
    count losses without re-deriving them from ``by_encounter``/``paths``."""

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
    #: Delivered chart files, in the index's own order.
    paths: list[Path]


@dataclass(frozen=True)
class _PatientAttachments:
    """One patient's carried source documents, as both consumers need them."""

    #: Patient-page rows: ``{name, title, pages}``, one per named document.
    rows: list[dict[str, object]]
    #: What the FHIR bundle carries, keyed by ``DocumentArtifact`` id.
    by_doc: dict[str, DeliveredAttachment]
    #: Delivered files on disk, one entry per named document.
    paths: list[Path]


@dataclass(frozen=True)
class BundleResult:
    """What landed on disk for one patient."""

    patient_id: str
    out_dir: Path
    bundle_path: Path
    pdf_paths: list[Path] = field(default_factory=list)
    #: Source documents (scans, lab reports) the charts reference — separate
    #: from ``pdf_paths`` so either going missing is never hidden by the other.
    attachment_paths: list[Path] = field(default_factory=list)
    qa_report_path: Path | None = None
    readme_path: Path | None = None
    #: Charts the render index named for this patient but were not on disk
    #: when built — travels with the result rather than being filtered away.
    missing_count: int = 0


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
    #: The search index, or None under a grouping that has no cross-patient page.
    index_path: Path | None = None
    #: Charts the index named that never landed (missing file or failed
    #: copy) — the difference between "two visits" and "a lost chart".
    missing_count: int = 0
    #: Charts filed under ``unattributed/`` rather than guessed onto a
    #: patient — not a loss, but nobody opens that folder unasked.
    unattributed_count: int = 0
    #: One row per delivered patient directory, in the order they were written.
    patients: list[BundleResult] = field(default_factory=list)


@dataclass(frozen=True)
class _Run:
    """What one delivery run holds still while it walks its records."""

    out: Path
    pdfs_dir: Path | None
    attachments_dir: Path
    render_index: RenderIndex | None
    qa_report: QAReport | None
    qa_lookup: dict[str, str]
    generated_at: str
    claimed_dirs: dict[str, str]


@dataclass(frozen=True)
class _Delivered:
    """One patient's directory, plus what the run still needs from it."""

    result: BundleResult
    charts: _PatientCharts
    encounter_count: int


def _totals(out: Path, delivered: list[_Delivered]) -> ArchiveResult:
    """The run summary every grouping shares: what each patient directory
    landed, added up. The archive fills in its own index and sweep counts."""
    return ArchiveResult(
        out_dir=out,
        patient_count=len(delivered),
        encounter_count=sum(d.encounter_count for d in delivered),
        pdf_count=sum(len(d.result.pdf_paths) for d in delivered),
        attachment_count=sum(len(d.result.attachment_paths) for d in delivered),
        missing_count=sum(d.charts.absent for d in delivered),
        patients=[d.result for d in delivered],
    )


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
    """Render canonical records as a static, offline-readable tree."""

    def __init__(
        self, generator: str | None = None, *, grouping: Grouping = Grouping.ARCHIVE
    ) -> None:
        import anastomosis

        self.generator = generator or f"anastomosis {anastomosis.__version__}"
        self.grouping = grouping
        self._layout = _LAYOUTS[grouping]
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
        run = self._open_run(secure_output_dir(out_dir), pdfs_dir, qa_report)
        records_list = list(records)
        delivered = [self._deliver_patient(record, run) for record in records_list]
        if self.grouping is Grouping.BUNDLE:
            return _totals(run.out, delivered)
        return self._close_archive(run, records_list, delivered)

    def _open_run(self, out: Path, pdfs_dir: Path | None, qa_report: QAReport | None) -> _Run:
        """Harden the output root, load the render index, and say once when
        this grouping has no way to attribute the charts it can see."""
        render_index = RenderIndex.load(pdfs_dir)
        if self.grouping is Grouping.ARCHIVE:
            self._copy_assets(out)
        elif render_index is None and pdfs_dir is not None and pdfs_dir.is_dir():
            # No cross-patient sweep to catch them here, so the run says once
            # that no chart can be attributed at all.
            logger.warning("no render index; bundle will deliver without chart PDFs")
        return _Run(
            out=out,
            pdfs_dir=pdfs_dir,
            # The record alone can't say what name a document lands under, so
            # the attachments travel from wherever the charts were assembled.
            attachments_dir=(pdfs_dir or out) / ATTACHMENTS_DIRNAME,
            render_index=render_index,
            qa_report=qa_report,
            qa_lookup=_qa_lookup(qa_report),
            generated_at=_clock_now().isoformat(),
            # Per-run ledger: two ids that sanitize to one name, or two records
            # under one id, would otherwise merge into one exist_ok slot.
            claimed_dirs={},
        )

    def _close_archive(
        self, run: _Run, records: list[PatientRecord], delivered: list[_Delivered]
    ) -> ArchiveResult:
        """The cross-patient half: the unattributed sweep, the chart books, the
        search index and the archive's own README."""
        # Anything in ``pdfs_dir`` not claimed by an indexed patient lands
        # in ``unattributed/`` so nothing is silently dropped or guessed.
        owned = {name for d in delivered for name in d.charts.claimed_sources}
        unattributed, sweep_failures = self._route_unattributed_pdfs(
            run.pdfs_dir, run.render_index, owned, run.out
        )
        # A failed attributed copy leaves a chart unclaimed for the sweep to
        # settle; counting it in the per-patient totals too would double-count.
        totals = _totals(run.out, delivered)
        missing = totals.missing_count + sweep_failures
        _chart_conservation(
            run.render_index,
            records,
            run.pdfs_dir,
            delivered=totals.pdf_count,
            unattributed=unattributed,
            missing=missing,
        ).check()
        index_path = self._write_index(
            run.out,
            [
                _manifest_entry(record, d.result.patient_id)
                for record, d in zip(records, delivered, strict=True)
            ],
            encounter_count=totals.encounter_count,
            generated_at=run.generated_at,
        )
        self._write_readme(run.out / "README.txt", README_TEXT)
        logger.info(
            "archive delivered: %d patients, %d encounters, %d pdfs, "
            "%d attachments (%d missing, %d unattributed)",
            totals.patient_count,
            totals.encounter_count,
            totals.pdf_count,
            totals.attachment_count,
            missing,
            unattributed,
        )
        return replace(
            totals,
            index_path=index_path,
            missing_count=missing,
            unattributed_count=unattributed,
        )

    # --- one patient --------------------------------------------------------

    def _deliver_patient(self, record: PatientRecord, run: _Run) -> _Delivered:
        parent = run.out.joinpath(*self._layout.parent)
        # Budgeted against the tree being built, so a long source id can never
        # turn a delivered chart into a mid-run FileNotFoundError.
        pid = budgeted_name(
            record.patient.id, "unknown", parent=parent, reserve=self._layout.reserve
        )
        # The record is the witness: a patient id is not guaranteed unique,
        # so two records under one id would otherwise merge silently here.
        claim_delivered_name(
            run.claimed_dirs,
            pid,
            record.patient.id,
            kind="patient directory",
            content=record_witness(record),
        )
        patient_dir = parent / pid
        patient_dir.mkdir(parents=True, exist_ok=True)

        # Copied before the FHIR bundle: the record alone can't say what name
        # a document lands under, so the bundle needs what this measured.
        attachments = self._copy_patient_attachments(record, run.attachments_dir, patient_dir)
        # Carries what was just measured above, so every DocumentReference
        # resolves to a real file beside it.
        bundle_path = write_fhir_bundle(record, patient_dir, attachments.by_doc)
        # Attributed strictly via the render index (RULES.md 11), never a
        # name-prefix guess. Ownership tracks the SOURCE name (what the
        # unattributed sweep sees in ``pdfs_dir``), not the budgeted delivered
        # name, or an already-filed chart would re-copy there too.
        charts = self._copy_patient_charts(record, run, patient_dir)
        qa_path, readme_path = self._write_patient_extras(
            record, run, patient_dir, charts, attachments
        )
        return _Delivered(
            result=BundleResult(
                patient_id=pid,
                out_dir=patient_dir,
                bundle_path=bundle_path,
                pdf_paths=charts.paths,
                attachment_paths=attachments.paths,
                qa_report_path=qa_path,
                readme_path=readme_path,
                missing_count=charts.absent,
            ),
            charts=charts,
            encounter_count=len(record.encounters),
        )

    def _write_patient_extras(
        self,
        record: PatientRecord,
        run: _Run,
        patient_dir: Path,
        charts: _PatientCharts,
        attachments: _PatientAttachments,
    ) -> tuple[Path | None, Path | None]:
        """What sits beside one patient's bundle: HTML pages, or a QA slice
        and this patient's own README."""
        if self.grouping is Grouping.ARCHIVE:
            self._write_archive_pages(record, run, patient_dir, charts, attachments.rows)
            return None, None
        qa_path = self._write_qa_slice(record, patient_dir, run.qa_report)
        readme_path = patient_dir / "README.txt"
        self._write_readme(
            readme_path,
            BUNDLE_README_TEXT,
            patient_id=record.patient.id,
            generated_at=_clock_now().isoformat(),
            generator=self.generator,
        )
        logger.info(
            "bundle delivered for patient %s: %d pdfs, %d attachments, %d missing, qa=%s",
            safe_log_id(patient_dir.name),
            len(charts.paths),
            len(attachments.paths),
            charts.absent,
            "yes" if qa_path else "no",
        )
        return qa_path, readme_path

    def _write_archive_pages(
        self,
        record: PatientRecord,
        run: _Run,
        patient_dir: Path,
        charts: _PatientCharts,
        attachment_rows: list[dict[str, object]],
    ) -> None:
        (patient_dir / "encounters").mkdir(parents=True, exist_ok=True)
        # Ledger is fresh per patient: page names only need to be distinct
        # within this patient's own encounters/ directory.
        claimed_pages: dict[str, str] = {}
        for encounter in record.encounters:
            self._write_encounter_page(
                encounter,
                record,
                patient_dir,
                charts.by_encounter,
                run.qa_lookup,
                run.generated_at,
                claimed_pages,
                chart_missing=encounter.id in charts.missing,
            )
        self._write_patient_page(record, patient_dir, run.generated_at, attachment_rows)

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
        self, record: PatientRecord, attachments_dir: Path, patient_dir: Path
    ) -> _PatientAttachments:
        """Copy this patient's source documents; measure each for the FHIR
        rendition beside it.

        Two artifacts naming ONE file each get a row, but the copy and the
        hash are not doubled (:func:`measured_attachment` reuses the first)."""
        wanted = [doc for doc in record.documents if doc.path]
        if not wanted:
            return _PatientAttachments([], {}, [])

        out_dir = patient_dir / ATTACHMENTS_DIRNAME
        out_dir.mkdir(parents=True, exist_ok=True)
        sources = _attachment_sources(record, attachments_dir)
        delivered, failures = copy_claimed_charts(out_dir, sources, kind="attachment")
        for _name, failure in failures:
            logger.warning(
                "attachment could not be delivered for patient %s (%s)",
                safe_log_id(record.patient.id),
                failure,
            )

        rows: list[dict[str, object]] = []
        paths: list[Path] = []
        landed: dict[str, DeliveredAttachment] = {}  # delivered filename -> measurement
        by_doc: dict[str, DeliveredAttachment] = {}
        for doc in wanted:
            copied = delivered.get(Path(doc.path or "").name)
            if copied is None:
                continue
            rows.append({"name": copied, "title": doc.title or copied, "pages": doc.page_count})
            paths.append(out_dir / copied)
            # Two artifacts naming ONE source file measure it once
            # (`measured_attachment`), so both resolve to the file that exists.
            by_doc[doc.id] = measured_attachment(
                landed, out_dir / copied, f"{ATTACHMENTS_DIRNAME}/{copied}"
            )

        missing = len(wanted) - len(rows)
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
        return _PatientAttachments(rows, by_doc, paths)

    def _copy_patient_charts(
        self, record: PatientRecord, run: _Run, patient_dir: Path
    ) -> _PatientCharts:
        """Copy this patient's PDFs into ``pdfs/`` (RULES.md 11); no index
        entries means no PDFs.

        Destination naming (:func:`copy_claimed_charts`) is a hard failure; a
        chart the index names but never arrives is COUNTED, not just logged."""
        empty = _PatientCharts({}, set(), set(), 0, [])
        render_index, pdfs_dir = run.render_index, run.pdfs_dir
        if render_index is None or pdfs_dir is None or not pdfs_dir.is_dir():
            return empty
        names = render_index.for_patient(record.patient.id)
        if not names:
            return empty

        out_dir = patient_dir / "pdfs"
        out_dir.mkdir(parents=True, exist_ok=True)
        sources: list[tuple[str, Path]] = []
        absent: list[str] = []
        for name in names:
            source = pdfs_dir / name
            if source.is_file():
                sources.append((name, source))
                continue
            # The index claims a PDF the engine never wrote, or it was
            # deleted post-render; log the surrogate id only, never fake it.
            logger.warning(
                "indexed pdf missing on disk for patient %s", safe_log_id(record.patient.id)
            )
            absent.append(name)
        # Naming (not I/O) raises inside the loop: an unnameable destination
        # fails loud rather than leaving the chart out silently.
        delivered, failures = copy_claimed_charts(out_dir, sources, kind="chart")
        for _name, failure in failures:
            logger.warning("pdf copy failed (%s)", failure)
        lost = absent + [name for name, _failure in failures]
        return _charts_view(render_index, names, delivered, out_dir, lost, len(absent))

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
        # Budgeted and claimed exactly like an attributed chart: a PDF that
        # lands here is still a chart nobody may lose.
        delivered, failures = copy_claimed_charts(
            target, [(p.name, p) for p in orphans], kind="unattributed chart"
        )
        for _name, failure in failures:
            logger.warning("unattributed pdf copy failed (%s)", failure)
        return len(delivered), len(failures)

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

    def _write_qa_slice(
        self,
        record: PatientRecord,
        patient_dir: Path,
        qa_report: QAReport | None,
    ) -> Path | None:
        if qa_report is None:
            return None
        slice_docs = [doc for doc in qa_report.documents if _is_this_patients(doc, record)]
        payload = {
            "generated_at": _clock_now().isoformat(),
            "patient_id": record.patient.id,
            "summary": {v.value: sum(1 for d in slice_docs if d.verdict is v) for v in Verdict},
            "documents": [
                {
                    "file": doc.path.name,
                    "encounter_id": doc.encounter_id,
                    "verdict": doc.verdict.value,
                    "checks": [
                        {
                            "check": result.check,
                            "verdict": result.verdict.value,
                            "findings": result.findings,
                        }
                        for result in doc.results
                    ],
                }
                for doc in slice_docs
            ],
        }
        target = patient_dir / "qa_report.json"
        atomic_write_text(target, json.dumps(payload, indent=2))
        return target

    def _write_readme(self, target: Path, text: str, **fields: str) -> None:
        """The one README mechanism: a plain-text template formatted with the
        fields its grouping names (the archive's names none)."""
        # PHI-BY-DESIGN: the per-patient README names its patient id; the
        # caller already hardened the directory (RULES.md 18). See SECURITY.md.
        # codeql[py/clear-text-storage-sensitive-data]
        atomic_write_text(target, text.format(**fields))


# --- helpers ----------------------------------------------------------------


def _is_this_patients(doc: DocumentQA, record: PatientRecord) -> bool:
    """Whether a graded row belongs in ``record``'s bundle.

    A whole-patient page has no visit id; it carries the PATIENT id in that
    slot instead. Encounter ids alone dropped that row, turning a missing
    verdict into a false "nothing to say" (#399)."""
    return doc.encounter_id == record.patient.id or doc.encounter_id in {
        encounter.id for encounter in record.encounters
    }


def _attachment_sources(record: PatientRecord, attachments_dir: Path) -> list[tuple[str, Path]]:
    """``(filename, file)`` for every document this record names that
    ``attachments_dir`` actually holds. A name it does not hold is logged by
    surrogate id — never the filename — and left out, never guessed at."""
    sources: list[tuple[str, Path]] = []
    for doc in record.documents:
        if not doc.path:
            continue
        name = Path(doc.path).name
        source = attachments_dir / name
        if not source.is_file():
            # Reaching here means the charts directory was edited after the
            # run; the pipeline refuses this case outright.
            logger.warning(
                "record names an attachment missing from the charts directory for patient %s",
                safe_log_id(record.patient.id),
            )
            continue
        sources.append((name, source))
    return sources


def _charts_view(
    render_index: RenderIndex,
    names: tuple[str, ...],
    delivered: dict[str, str],
    out_dir: Path,
    lost: list[str],
    absent: int,
) -> _PatientCharts:
    """What one patient's chart copy produced, indexed the way the pages read it.

    ``lost`` names the source files that did not land, so the per-encounter page
    can say the chart is missing rather than quietly render without the link."""
    missing: set[str] = set()
    for name in lost:
        entry = render_index.lookup(name)
        if entry is not None:
            missing.add(entry.encounter_id)
    by_encounter: dict[str, str] = {}
    paths: list[Path] = []
    for name in names:
        landed = delivered.get(name)
        if landed is None:
            continue
        paths.append(out_dir / landed)
        entry = render_index.lookup(name)
        if entry is not None:
            # First-wins: a doubled encounter→pdf row (corrupted index) keeps
            # the first assignment, never overwrites.
            by_encounter.setdefault(entry.encounter_id, landed)
    return _PatientCharts(by_encounter, set(delivered), missing, absent, paths)


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
