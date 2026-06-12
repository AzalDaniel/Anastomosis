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
from anastomosis.core.logutil import exc_tag
from anastomosis.core.model import Encounter, Patient, PatientRecord
from anastomosis.core.output import secure_output_dir
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
        pdf_lookup = _index_pdfs(pdfs_dir)

        manifest_entries: list[dict[str, object]] = []
        encounter_count = 0
        pdf_count = 0
        generated_at = datetime.now(UTC).isoformat()

        records_list = list(records)
        qa_lookup = _qa_lookup(qa_report)

        for record in records_list:
            pid = _safe_id(record.patient.id, "unknown")
            patient_dir = out / "patients" / pid
            (patient_dir / "encounters").mkdir(parents=True, exist_ok=True)

            # FHIR R4 Bundle — the machine-readable rendition.
            bundle = to_bundle(record)
            (patient_dir / "bundle.json").write_text(
                json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8"
            )

            # PDFs — only those that belong to this patient, named by the
            # engine's deterministic pattern; PDFs we can't attribute stay
            # unowned (logged in the archive's README, not silently dropped).
            patient_pdfs = self._copy_patient_pdfs(record, pdf_lookup, patient_dir)
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

        index_path = self._write_index(
            out,
            manifest_entries,
            encounter_count=encounter_count,
            generated_at=generated_at,
        )
        self._write_readme(out)
        logger.info(
            "archive delivered: %d patients, %d encounters, %d pdfs",
            len(manifest_entries),
            encounter_count,
            pdf_count,
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
        pdf_lookup: dict[str, list[Path]],
        patient_dir: Path,
    ) -> dict[str, str]:
        """Copy this patient's PDFs into the patient's own ``pdfs/`` slot.

        Returns a mapping of ``encounter.id -> pdf filename`` so the
        per-encounter pages can link to the right file. PDFs are matched by
        the engine's filename prefix (``{family}_{given}_``); patient ids
        never appear inside the chart filenames so this is the only way to
        attribute a PDF without coupling to the engine."""
        if not pdf_lookup:
            return {}
        prefix = _patient_prefix(record.patient)
        if not prefix:
            return {}
        candidates = pdf_lookup.get(prefix, [])
        if not candidates:
            return {}

        out_dir = patient_dir / "pdfs"
        out_dir.mkdir(parents=True, exist_ok=True)
        mapping: dict[str, str] = {}
        copied: set[str] = set()
        for source in candidates:
            try:
                shutil.copyfile(source, out_dir / source.name)
                copied.add(source.name)
            except OSError as exc:
                logger.warning("pdf copy failed (%s)", exc_tag(exc))

        # Best-effort encounter→file assignment: match by DOS in the
        # filename. The engine pattern is ``{family}_{given}_{dos}_{type}``
        # so the date appears as a stable substring per encounter.
        for encounter in record.encounters:
            if encounter.date_of_service is None:
                continue
            dos = encounter.date_of_service.strftime("%m-%d-%Y")
            matches = sorted(name for name in copied if dos in name)
            if not matches:
                continue
            # Collisions: the engine suffixes the second filename with a
            # short hash of the encounter id. Prefer that one if it matches.
            suffix = encounter.id.replace("-", "")[:8]
            for name in matches:
                if suffix and suffix in name:
                    mapping[encounter.id] = name
                    break
            else:
                mapping.setdefault(encounter.id, matches[0])
        return mapping

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
        index_path.write_text(html, encoding="utf-8")
        (out / "index.json").write_text(
            json.dumps(manifest_entries, indent=2, sort_keys=True), encoding="utf-8"
        )
        return index_path

    def _write_readme(self, out: Path) -> None:
        readme = out / "README.txt"
        readme.write_text(README_TEXT, encoding="utf-8")


# --- helpers ----------------------------------------------------------------


def _patient_prefix(patient: Patient) -> str:
    family = re.sub(r"[^A-Za-z0-9_-]+", "_", (patient.family_name or "").strip()).strip("_")
    given = re.sub(r"[^A-Za-z0-9_-]+", "_", (patient.given_name or "").strip()).strip("_")
    if not (family and given):
        return ""
    return f"{family}_{given}_"


def _index_pdfs(pdfs_dir: Path | None) -> dict[str, list[Path]]:
    """Build a prefix→files index over the rendered PDF directory.

    The engine names each chart ``{family}_{given}_{dos}_{type}.pdf``, so
    grouping by the leading ``family_given_`` prefix gives every chart that
    might belong to one patient with one cheap scan. Patients that share
    a family+given name (synthetic-fixture collisions, or unrelated patients
    with the same name) will need a tighter match later — for v0.1 the
    fixture data has no such collisions.
    """
    if pdfs_dir is None or not pdfs_dir.is_dir():
        return {}
    index: dict[str, list[Path]] = {}
    for pdf in sorted(pdfs_dir.glob("*.pdf")):
        # Prefix = up to the second underscore (family + "_" + given + "_").
        parts = pdf.name.split("_")
        if len(parts) < 3:
            continue
        prefix = f"{parts[0]}_{parts[1]}_"
        index.setdefault(prefix, []).append(pdf)
    return index


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
