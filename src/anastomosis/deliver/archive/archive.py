"""Offline archive deliverer — Archivist persona.

Produces a static, browsable directory tree from canonical PatientRecords:

* one ``index.html`` with a search box and an inline-JSON patient manifest,
* one ``patients/<patient_id>/`` subtree per patient containing the human
  HTML summary, the machine-readable FHIR R4 Bundle JSON, and any rendered
  chart PDFs that belong to that patient,
* a single ``assets/`` directory with the stylesheet and the search bootstrap.

Design contract (all enforced by tests):

* **Zero network at read time.** Every HTML page declares a strict CSP and
  references assets via relative paths only — the archive opens from a
  ``file://`` URL with no outbound requests.
* **No inline executable JavaScript.** The only ``<script>`` blocks are
  ``type="application/json"`` (data) on ``index.html`` and a single
  ``<script src="assets/anast-index.js">`` (self-served).
* **ID-based folder naming** so renaming a patient never moves their files.
  Human-readable labels live INSIDE the HTML and the manifest JSON.
* **Dual format per patient** — HTML for humans, FHIR R4 Bundle JSON for
  machines, plus the rendered chart PDFs. PDF/A upgrade is M6 work.
* **PHI hygiene** — the output directory is hardened by
  :func:`anastomosis.core.output.secure_output_dir`; logging emits counts
  and ids only, never patient-derived strings.

``index.json`` manifest shape (one entry per patient)::

    {
      "id": str,                  # patient.id, also the directory name
      "display_name": str,        # human label shown in the patient list
      "dob": str | None,          # ISO YYYY-MM-DD if known
      "encounter_count": int,
      "search": str               # concatenated lowercased searchable text:
                                  # name + dob + chief complaints + note text
                                  # shadows — what the search bootstrap matches
                                  # tokens against
    }

Reader's note: every chart already has its date and provider IDs in the
emitted PDF; this manifest exists only for the in-browser search box.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from anastomosis.core.atomic import atomic_copy, atomic_write_text
from anastomosis.core.conservation import Conservation
from anastomosis.core.logutil import safe_log_id
from anastomosis.core.model import Encounter, PatientRecord
from anastomosis.core.output import secure_output_dir
from anastomosis.core.textutil import HASH_TAG_CHARS, budgeted_name
from anastomosis.deliver._shared import (
    claim_delivered_name,
    copy_claimed_chart,
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
# Room kept free under each patient directory for its deepest child. EVERY
# child is itself budgeted — the per-encounter page ``encounters/<id>.html``
# and the copied chart ``pdfs/<chart>.pdf`` — so what has to be reserved is not
# a plausible child NAME but the room a budgeted child needs to still come out
# DISTINCT: the longest fixed wrapper (``/encounters/`` + ``.html``) plus the
# shortest distinct name :func:`budgeted_name` can return, its hash tag. Less
# than that and the child budget raises; more than that and a patient id is cut
# shorter than it needs to be. A patient id long enough to consume the rest is
# cut (with its hash tag) instead — better a shortened directory name than a
# tree whose pages cannot be written at all.
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

    ``missing`` is the point of the type. Two of the three fields were already
    returned as a bare tuple; the third was discovered in the same loop, logged,
    and dropped, so no caller could count it.
    """

    #: encounter id -> the DELIVERED filename its chart was written under.
    by_encounter: dict[str, str]
    #: the renderer's own filenames this patient claimed, which is what the
    #: caller's unattributed sweep matches against (it reads the charts
    #: directory, where the names have not been budgeted yet).
    claimed_sources: set[str]
    #: encounter ids with no chart in THIS patient's folder, whichever way it
    #: went wrong. Drives the per-encounter page, which links into that folder.
    missing: set[str]
    #: how many of those charts were not in the charts directory at all.
    #: Counted apart from the rest because it decides who does the counting: a
    #: chart whose file is missing is gone here and nowhere else will see it,
    #: while a chart whose copy failed is still in the charts directory, so it
    #: reaches the unattributed sweep and is reconciled there. Adding both here
    #: counted one failed chart twice.
    absent: int


@dataclass(frozen=True)
class ArchiveResult:
    """What landed on disk, summarized for the CLI."""

    out_dir: Path
    patient_count: int
    encounter_count: int
    pdf_count: int
    #: Source attachments delivered — the scans and lab reports a chart
    #: references. Counted separately from `pdf_count`, which is charts this
    #: run rendered: an operator needs to see that the documents came through
    #: too, and one number covering both would hide either going missing.
    attachment_count: int
    index_path: Path
    #: Charts that were expected in the delivered tree and are not in it —
    #: the index named one and the file was gone, or a copy failed (including
    #: in the unattributed sweep). A number the operator has to see: it is the
    #: difference between "this patient had two visits" and "this patient's
    #: second chart is lost".
    missing_count: int = 0
    #: Charts that arrived but belong to no patient in this run, so they were
    #: filed under ``unattributed/`` rather than guessed onto someone. Not a
    #: loss — they are on disk — but nobody will open that directory unless
    #: they are told it has something in it.
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
    """The canonical -> delivered seam: every chart this run has to answer for
    ends somewhere nameable.

    Two sources of obligation, and the union of them is what must balance:

    * every chart the render index NAMES for a patient being delivered — the
      run said it produced these, so their absence from the archive is a loss
      even though no file is malformed;
    * every PDF actually sitting in the charts directory — including ones the
      index does not name, which is how a chart written by an earlier run, or
      by a run whose index write failed, still gets accounted for instead of
      being left behind.

    Three dispositions and no fourth: copied to its patient, swept into
    ``unattributed/`` because nothing could attribute it, or counted missing.
    This is the shape #110 walked through — five attachments in the export and
    zero in the output, with every artifact check passing because each artifact
    that existed was fine.
    """
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
        generated_at = datetime.now(UTC).isoformat()

        records_list = list(records)
        qa_lookup = _qa_lookup(qa_report)
        owned_pdfs: set[str] = set()
        # Per-run ledger of delivered directory name -> the patient id that
        # claimed it. Two ids that sanitize to ONE name (``MRN 1234`` and
        # ``MRN/1234`` both collapse to ``MRN_1234``) would otherwise merge into
        # a single ``patients/<id>/`` slot, because every writer below is
        # exist_ok/overwrite. A second claimant is a hard failure.
        claimed_dirs: dict[str, str] = {}

        for record in records_list:
            # Budgeted against the tree this writer is about to build: the
            # component is capped AND the full path stays inside the Windows
            # path budget, so a long source id can never turn a delivered
            # chart into a FileNotFoundError halfway through the archive.
            pid = budgeted_name(
                record.patient.id,
                "unknown",
                parent=out / "patients",
                reserve=_PATIENT_CHILD_RESERVE,
            )
            claim_delivered_name(claimed_dirs, pid, record.patient.id, kind="patient directory")
            patient_dir = out / "patients" / pid
            (patient_dir / "encounters").mkdir(parents=True, exist_ok=True)

            # FHIR R4 Bundle — the machine-readable rendition (the shared
            # deliverer mechanic; its PHI-BY-DESIGN rationale lives there).
            write_fhir_bundle(record, patient_dir)

            # PDFs — attributed strictly via the render index (patient_id
            # match; see render_index.py for why name-prefix guessing was
            # unsafe). No index entry -> unattributed/, never a guess (see
            # :meth:`_route_unattributed_pdfs` below).
            # Two different name sets come back: the DELIVERED names (budgeted,
            # what the pages link to) and the SOURCE names this patient claimed.
            # Ownership is tracked by SOURCE name because that is what the
            # unattributed sweep below sees in ``pdfs_dir`` — matching it
            # against a budgeted-shorter delivered name would re-copy a chart
            # already filed with its patient into ``unattributed/`` as well.
            charts = self._copy_patient_pdfs(record, render_index, pdfs_dir, patient_dir)
            patient_pdfs = charts.by_encounter
            owned_pdfs.update(charts.claimed_sources)
            pdf_count += len(patient_pdfs)
            missing_count += charts.absent

            # The source's own documents, handed to the patient whose record
            # names them. Charts and attachments are counted apart: one number
            # covering both would hide either going missing.
            patient_attachments = self._copy_patient_attachments(
                record, (pdfs_dir or out) / ATTACHMENTS_DIRNAME, patient_dir
            )
            attachment_count += len(patient_attachments)

            # Per-encounter HTML pages. Ledger is fresh per patient: page names
            # only need to be distinct within this patient's own encounters/
            # directory, mirroring the chart ledger's per-patient scope below.
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

            # Patient summary page.
            self._write_patient_page(record, patient_dir, generated_at, patient_attachments)

            manifest_entries.append(_manifest_entry(record, pid))

        # Anything in ``pdfs_dir`` not claimed by an indexed patient lands
        # in ``unattributed/`` so nothing is silently dropped or guessed.
        unattributed_count, sweep_failures = self._route_unattributed_pdfs(
            pdfs_dir, render_index, owned_pdfs, out
        )
        # The sweep is where a chart still IN the charts directory is settled:
        # a failed attributed copy leaves the chart unclaimed, so the sweep sees
        # it and either rescues it into unattributed/ or cannot, and only then
        # is it missing. Counting it above as well would count one chart twice.
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
    ) -> list[dict[str, object]]:
        """Copy this patient's source attachments into their own slot.

        These are not charts this run rendered — they are the files the source
        export carried: a scanned referral, a lab report, the pages a chart
        references and is incomplete without. The run put them in the charts
        directory (see `pipeline._carry_attachments`); this hands each one to
        the patient whose record names it.

        Attribution needs no index, unlike the PDFs above. A chart's filename
        carries no patient id, so ownership had to be looked up; a document is
        named BY the record that owns it, so the record is the attribution.

        Returns what the patient page needs to link them. Two of this
        patient's documents claiming one delivered name still raises through
        the shared ledger — that is a wrong-chart hazard, not a missing file.
        """
        wanted = [doc for doc in record.documents if doc.path]
        if not wanted:
            return []

        out_dir = patient_dir / ATTACHMENTS_DIRNAME
        out_dir.mkdir(parents=True, exist_ok=True)
        claims: dict[str, str] = {}
        delivered: list[dict[str, object]] = []
        for doc in wanted:
            name = Path(doc.path or "").name
            source = attachments_dir / name
            if not source.is_file():
                # The record names a document the run did not carry. The
                # pipeline refuses that case outright, so reaching here means a
                # charts directory was edited between the run and delivery.
                # Loud by surrogate id — never the filename, which is a source
                # identifier, and never a patient value.
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

        missing = len(wanted) - len(delivered)
        if missing:
            # Deliberately a warning here, not a refusal. Conservation belongs
            # to the run: `pipeline._carry_attachments` knows the export, checks
            # every attachment arrived, and stops the run if one did not. This
            # deliverer only sees a charts directory, so a record naming a
            # document that is not in it means the directory was assembled
            # without the carry step or edited after it — a state the pipeline
            # cannot produce. Refusing here would put a precondition on
            # `deliver()` that its one production caller already satisfies, and
            # would fail an operator delivering an older charts directory.
            # Counted, so `attachment_count` never overstates what landed.
            logger.warning(
                "%d attachment(s) named by a record are not in the charts directory for patient %s",
                missing,
                safe_log_id(record.patient.id),
            )
        return delivered

    def _copy_patient_pdfs(
        self,
        record: PatientRecord,
        render_index: RenderIndex | None,
        pdfs_dir: Path | None,
        patient_dir: Path,
    ) -> _PatientCharts:
        """Copy this patient's PDFs into the patient's own ``pdfs/`` slot.

        Attribution is strictly index-based: only PDFs the engine actually
        wrote for ``record.patient.id`` are copied, and the encounter link comes
        from the index entry's ``encounter_id`` (not a substring match
        on the date in the filename). A patient with no index entries
        gets no PDFs — never a guess.

        The DELIVERED name is budgeted (:func:`~anastomosis.deliver._shared.
        copy_claimed_chart`), because the renderer's
        ``{family}_{given}_{dos}_{type}.pdf`` is bounded only by ``safe_name``
        — far past what the Windows path budget can hold under a deep output
        tree. An over-budget destination is a hard failure here, never a
        warn-and-continue: this is the path that carries the charts.

        A chart the index names and that does not arrive is COUNTED, not just
        logged: the count is what reaches the operator, and the log line only
        reaches whoever is already reading logs.
        """
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
                # The index claims a PDF the engine never wrote (or it was
                # deleted post-render). Log loud by the run-scoped surrogate —
                # never the filename, which embeds the patient name and date of
                # service, and never the raw source GUID — and never silently
                # fake an attribution.
                logger.warning(
                    "indexed pdf missing on disk for patient %s", safe_log_id(record.patient.id)
                )
                lost(name)
                absent += 1
                continue
            # OUTSIDE the copy's warn path on purpose: a destination that
            # cannot be named distinctly raises (ValueError) rather than
            # leaving the chart out of the delivered tree (copy_claimed_chart
            # budgets before it copies). Permission/disk failures keep the
            # existing warn-and-continue below.
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
        """Copy any leftover PDFs into ``out/unattributed/`` (fail-closed).

        Two cases land here: PDFs in ``pdfs_dir`` that the index does not
        mention (a stray file not produced by THIS run), and the entire
        directory when no index is present at all. In both cases the
        deliverer refuses to guess — the PDFs are visible to the operator
        in one place, never silently dropped, never silently misattributed.
        Returns ``(copied, failed)``. Keeping the two apart is the point: a
        chart whose copy failed here is not "unattributed", it is a chart that
        was in the folder and is not in the archive, so it belongs to the run's
        missing count and not to the count of files someone can go and read.
        """
        if pdfs_dir is None or not pdfs_dir.is_dir():
            return 0, 0
        all_pdfs = sorted(p for p in pdfs_dir.glob("*.pdf"))
        if not all_pdfs:
            return 0, 0
        if render_index is None:
            # No index at all → every PDF is unattributed by the same
            # fail-closed rule. Log loud (by count only — the pdfs dir is a
            # path under the output tree) so the missing sidecar is visible.
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
            # The absence of a chart link means two different things and the
            # page has to tell them apart. An encounter the run never rendered
            # (a selection rule kept it out) has no chart and never had one;
            # this one was rendered and did not arrive. Only the second is a
            # loss, and only the second gets a sentence.
            chart_missing=chart_missing,
            qa_verdict=qa_lookup.get(encounter.id),
            sections=sections_ctx,
            addenda=addenda_ctx,
            generator=self.generator,
            generated_at=generated_at,
        )
        page_id = _encounter_page_id(patient_dir, encounter)
        # Two encounter ids that sanitize/truncate alike would otherwise
        # overwrite one encounter's page with another's (exist_ok write below).
        # The rendered page goes in as the claim's witness because encounter
        # ids are not guaranteed unique: a C-CDA may list two <encounter>
        # entries under one <id root>, and the parser keeps a vendor GUID
        # verbatim. Two visits, one id, one slot — without the witness the
        # second page silently replaces the first while the run reports two.
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
        # ``json.dumps`` does not escape ``<``, so a chart title containing
        # ``</script>`` would close the tag and break out of the JSON island.
        # Escaping every ``</`` sequence is JSON-equivalent and closes that.
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
        # PHI-BY-DESIGN: the archive index and its JSON manifest name patients so
        # the offline search box works; same secure_output_dir hardening as
        # write_fhir_bundle (_shared.py); see SECURITY.md.
        # codeql[py/clear-text-storage-sensitive-data]
        atomic_write_text(index_path, html)
        # PHI-BY-DESIGN: same hardened-directory guarantee as the index above
        # (see SECURITY.md, "Code scanning & suppression policy (auditable)").
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

    ONE definition, called by both the patient page (which links
    ``encounters/<id>.html``) and the encounter writer (which creates that
    file): a second, differently-budgeted derivation would produce a link that
    points at nothing — a chart the operator cannot reach from the archive.
    """
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
