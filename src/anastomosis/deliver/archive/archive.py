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
from anastomosis.core.logutil import safe_log_id
from anastomosis.core.model import Encounter, PatientRecord
from anastomosis.core.output import secure_output_dir
from anastomosis.core.textutil import HASH_TAG_CHARS, budgeted_name
from anastomosis.deliver._shared import claim_delivered_name, copy_claimed_chart, write_fhir_bundle
from anastomosis.deliver.render_index import RenderIndex
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
class ArchiveResult:
    """What landed on disk, summarized for the CLI."""

    out_dir: Path
    patient_count: int
    encounter_count: int
    pdf_count: int
    index_path: Path


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
            patient_pdfs, claimed_sources = self._copy_patient_pdfs(
                record, render_index, pdfs_dir, patient_dir
            )
            owned_pdfs.update(claimed_sources)
            pdf_count += len(patient_pdfs)

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
                )

            # Patient summary page.
            self._write_patient_page(record, patient_dir, generated_at)

            manifest_entries.append(_manifest_entry(record, pid))

        # Anything in ``pdfs_dir`` not claimed by an indexed patient lands
        # in ``unattributed/`` so nothing is silently dropped or guessed.
        unattributed_count = self._route_unattributed_pdfs(pdfs_dir, render_index, owned_pdfs, out)

        index_path = self._write_index(
            out,
            manifest_entries,
            encounter_count=encounter_count,
            generated_at=generated_at,
        )
        self._write_readme(out)
        logger.info(
            "archive delivered: %d patients, %d encounters, %d pdfs (%d unattributed)",
            len(manifest_entries),
            encounter_count,
            pdf_count,
            unattributed_count,
        )
        return ArchiveResult(
            out_dir=out,
            patient_count=len(manifest_entries),
            encounter_count=encounter_count,
            pdf_count=pdf_count,
            index_path=index_path,
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

    def _copy_patient_pdfs(
        self,
        record: PatientRecord,
        render_index: RenderIndex | None,
        pdfs_dir: Path | None,
        patient_dir: Path,
    ) -> tuple[dict[str, str], set[str]]:
        """Copy this patient's PDFs into the patient's own ``pdfs/`` slot.

        Returns ``(encounter.id -> DELIVERED pdf filename, the SOURCE filenames
        this patient claimed)``. The first drives the per-encounter page links;
        the second is what the caller's unattributed sweep matches against,
        because that sweep looks at ``pdfs_dir``, where the names are still the
        renderer's — a budgeted delivered name would not be found there and the
        chart would be copied a second time into ``unattributed/``.

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
        """
        if render_index is None or pdfs_dir is None or not pdfs_dir.is_dir():
            return {}, set()
        names = render_index.for_patient(record.patient.id)
        if not names:
            return {}, set()

        out_dir = patient_dir / "pdfs"
        out_dir.mkdir(parents=True, exist_ok=True)
        mapping: dict[str, str] = {}
        claimed: dict[str, str] = {}
        claimed_sources: set[str] = set()
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
                continue
            # OUTSIDE the copy's warn path on purpose: a destination that
            # cannot be named distinctly raises (ValueError) rather than
            # leaving the chart out of the delivered tree (copy_claimed_chart
            # budgets before it copies). Permission/disk failures keep the
            # existing warn-and-continue below.
            delivered, failure = copy_claimed_chart(out_dir, claimed, source, name, kind="chart")
            if failure is not None:
                logger.warning("pdf copy failed (%s)", failure)
                continue
            assert delivered is not None  # copy_claimed_chart: failure is None => delivered isn't
            claimed_sources.add(name)
            entry = render_index.lookup(name)
            if entry is not None:
                # First-wins: a doubled encounter→pdf row (corrupted index)
                # keeps the first assignment, never overwrites.
                mapping.setdefault(entry.encounter_id, delivered)
        return mapping, claimed_sources

    def _route_unattributed_pdfs(
        self,
        pdfs_dir: Path | None,
        render_index: RenderIndex | None,
        owned: set[str],
        out: Path,
    ) -> int:
        """Copy any leftover PDFs into ``out/unattributed/`` (fail-closed).

        Two cases land here: PDFs in ``pdfs_dir`` that the index does not
        mention (a stray file not produced by THIS run), and the entire
        directory when no index is present at all. In both cases the
        deliverer refuses to guess — the PDFs are visible to the operator
        in one place, never silently dropped, never silently misattributed.
        Returns the count of PDFs actually copied (a failed copy is warned
        about, per :func:`~anastomosis.deliver._shared.copy_claimed_chart`,
        and excluded from the count) for the run summary.
        """
        if pdfs_dir is None or not pdfs_dir.is_dir():
            return 0
        all_pdfs = sorted(p for p in pdfs_dir.glob("*.pdf"))
        if not all_pdfs:
            return 0
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
            return 0
        target = out / "unattributed"
        target.mkdir(parents=True, exist_ok=True)
        claimed: dict[str, str] = {}
        copied = 0
        for source in orphans:
            # Budgeted and claimed exactly like an attributed chart: a PDF that
            # lands here is still a chart nobody may lose.
            _delivered, failure = copy_claimed_chart(
                target, claimed, source, source.name, kind="unattributed chart"
            )
            if failure is not None:
                logger.warning("unattributed pdf copy failed (%s)", failure)
                continue
            copied += 1
        return copied

    def _write_patient_page(
        self,
        record: PatientRecord,
        patient_dir: Path,
        generated_at: str,
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
            qa_verdict=qa_lookup.get(encounter.id),
            sections=sections_ctx,
            addenda=addenda_ctx,
            generator=self.generator,
            generated_at=generated_at,
        )
        page_id = _encounter_page_id(patient_dir, encounter)
        # Two encounter ids that sanitize/truncate alike would otherwise
        # overwrite one encounter's page with another's (exist_ok write below).
        claim_delivered_name(claimed_pages, page_id, encounter.id, kind="encounter page")
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
