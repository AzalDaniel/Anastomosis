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
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from anastomosis.core.fhir import to_bundle
from anastomosis.core.logutil import exc_tag, safe_log_id
from anastomosis.core.model import Encounter, PatientRecord
from anastomosis.core.output import secure_output_dir
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


def _safe_id(value: str, fallback: str) -> str:
    """Filesystem-safe directory name.

    Mirrors :func:`anastomosis.reconstruct.engine._safe_name` so that
    ``feedface-`` GUIDs (the synthetic fixture prefix) and any plain ASCII
    id pass through unchanged, and an exotic id never escapes its slot.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (value or "").strip()).strip("_")
    return cleaned or fallback


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

        for record in records_list:
            pid = _safe_id(record.patient.id, "unknown")
            patient_dir = out / "patients" / pid
            (patient_dir / "encounters").mkdir(parents=True, exist_ok=True)

            # FHIR R4 Bundle — the machine-readable rendition.
            bundle = to_bundle(record)
            # PHI-BY-DESIGN: writing the patient's FHIR record to disk IS the
            # product. ``patient_dir`` sits under a secure_output_dir-hardened
            # tree (0o700 owner-only on POSIX; on Windows NTFS, inheritance
            # stripped and access limited to the current user, SYSTEM, and
            # Administrators) with a PHI-warning README. See SECURITY.md, "Code
            # scanning & suppression policy (auditable)".
            # codeql[py/clear-text-storage-sensitive-data]
            (patient_dir / "bundle.json").write_text(
                json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8"
            )

            # PDFs — attributed strictly via the render index (patient_id
            # match). The old fallback that guessed ownership from
            # ``{family}_{given}_`` filename prefixes is gone: it cross-
            # leaked between two same-name patients. With no index present
            # the deliverer routes every PDF into ``unattributed/`` instead
            # of guessing (see :meth:`_route_unattributed_pdfs` below).
            patient_pdfs = self._copy_patient_pdfs(record, render_index, pdfs_dir, patient_dir)
            owned_pdfs.update(patient_pdfs.values())
            pdf_count += len(patient_pdfs)

            # Per-encounter HTML pages.
            encounter_count += len(record.encounters)
            for encounter in record.encounters:
                self._write_encounter_page(
                    encounter,
                    record,
                    patient_dir,
                    patient_pdfs,
                    qa_lookup,
                    generated_at,
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
            shutil.copyfile(source, assets_dir / name)
        notice = _ASSETS_DIR / "NOTICE.txt"
        if notice.is_file():
            licenses_dir = out / "LICENSES"
            licenses_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(notice, licenses_dir / "NOTICE.txt")

    def _copy_patient_pdfs(
        self,
        record: PatientRecord,
        render_index: RenderIndex | None,
        pdfs_dir: Path | None,
        patient_dir: Path,
    ) -> dict[str, str]:
        """Copy this patient's PDFs into the patient's own ``pdfs/`` slot.

        Returns a mapping of ``encounter.id -> pdf filename`` so the
        per-encounter pages can link to the right file. Attribution is
        strictly index-based: only PDFs the engine actually wrote for
        ``record.patient.id`` are copied, and the encounter link comes
        from the index entry's ``encounter_id`` (not a substring match
        on the date in the filename). A patient with no index entries
        gets no PDFs — never a guess.
        """
        if render_index is None or pdfs_dir is None or not pdfs_dir.is_dir():
            return {}
        names = render_index.for_patient(record.patient.id)
        if not names:
            return {}

        out_dir = patient_dir / "pdfs"
        out_dir.mkdir(parents=True, exist_ok=True)
        mapping: dict[str, str] = {}
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
            try:
                shutil.copyfile(source, out_dir / name)
            except OSError as exc:
                logger.warning("pdf copy failed (%s)", exc_tag(exc))
                continue
            entry = render_index.lookup(name)
            if entry is not None:
                # First-wins: a doubled encounter→pdf row (corrupted index)
                # keeps the first assignment, never overwrites.
                mapping.setdefault(entry.encounter_id, name)
        return mapping

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
        Returns the count for the run summary.
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
        for source in orphans:
            try:
                shutil.copyfile(source, target / source.name)
            except OSError as exc:
                logger.warning("unattributed pdf copy failed (%s)", exc_tag(exc))
        return len(orphans)

    def _write_patient_page(
        self,
        record: PatientRecord,
        patient_dir: Path,
        generated_at: str,
    ) -> None:
        encounters_ctx = [
            {
                "safe_id": _safe_id(enc.id, "encounter"),
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
        (patient_dir / "index.html").write_text(html, encoding="utf-8")

    def _write_encounter_page(
        self,
        encounter: Encounter,
        record: PatientRecord,
        patient_dir: Path,
        patient_pdfs: dict[str, str],
        qa_lookup: dict[str, str],
        generated_at: str,
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
        encounter_file = patient_dir / "encounters" / f"{_safe_id(encounter.id, 'encounter')}.html"
        encounter_file.write_text(html, encoding="utf-8")

    def _write_index(
        self,
        out: Path,
        manifest_entries: list[dict[str, object]],
        *,
        encounter_count: int,
        generated_at: str,
    ) -> Path:
        # json.dumps escapes </script> via the </ → </ path by
        # default? No — only `<` is unconditionally escaped (no it isn't in
        # python's default). Be explicit so a chart title containing
        # ``</script>`` can never break the inline JSON block.
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
        # the offline search box works; both land under a secure_output_dir-
        # hardened directory (0o700 owner-only on POSIX; on Windows NTFS,
        # inheritance stripped and access limited to the current user, SYSTEM, and
        # Administrators) with a PHI-warning README. See SECURITY.md, "Code
        # scanning & suppression policy (auditable)".
        # codeql[py/clear-text-storage-sensitive-data]
        index_path.write_text(html, encoding="utf-8")
        # PHI-BY-DESIGN: same hardened-directory guarantee as the index above
        # (see SECURITY.md, "Code scanning & suppression policy (auditable)").
        # codeql[py/clear-text-storage-sensitive-data]
        (out / "index.json").write_text(
            json.dumps(manifest_entries, indent=2, sort_keys=True), encoding="utf-8"
        )
        return index_path

    def _write_readme(self, out: Path) -> None:
        readme = out / "README.txt"
        readme.write_text(README_TEXT, encoding="utf-8")


# --- helpers ----------------------------------------------------------------


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
