"""Persisted index of (pdf_filename -> patient_id, encounter_id).

Attribution is index-only, never filename-prefix inference (RULES.md 11).
The engine writes ``render_index.json`` beside the PDFs; archive/bundle
load it before attributing. An unindexed PDF is kept but filed under
``unattributed/``, never guessed onto a patient.

JSON: ``{"version": 1, "entries": [{"pdf", "patient_id", "encounter_id"}]}``
(sorted for deterministic byte output)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anastomosis.core.atomic import atomic_write_text
from anastomosis.core.logutil import exc_tag

__all__ = ["INDEX_FILENAME", "RenderEntry", "RenderIndex", "RenderIndexConflict"]

logger = logging.getLogger(__name__)


class RenderIndexConflict(Exception):
    """One PDF claimed by two different encounters — a chart was overwritten.

    Carries the filename, which embeds a patient name and a date of service,
    so callers name the index rather than the file in any log line.
    """

    def __init__(self, pdf: str) -> None:
        super().__init__("one PDF is claimed by two different encounters")
        self.pdf = pdf


INDEX_FILENAME = "render_index.json"
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RenderEntry:
    """One row in the index: filename → owning patient + encounter."""

    pdf: str
    patient_id: str
    encounter_id: str


@dataclass(frozen=True)
class RenderIndex:
    """The deliverer-facing view of the engine's render-time truth.

    ``_by_name``/``_by_patient`` are built once at construction; lookups
    are O(1)."""

    entries: tuple[RenderEntry, ...]
    _by_name: dict[str, RenderEntry]
    _by_patient: dict[str, tuple[str, ...]]

    @classmethod
    def from_entries(cls, entries: Iterable[RenderEntry]) -> RenderIndex:
        items = tuple(sorted(entries, key=lambda e: e.pdf))
        by_name: dict[str, RenderEntry] = {}
        by_patient: dict[str, list[str]] = {}
        for entry in items:
            # One file, two encounters means a chart was overwritten before
            # this ran; refuse here, the index is the only place it's visible.
            claimed = by_name.get(entry.pdf)
            if claimed is not None and claimed != entry:
                raise RenderIndexConflict(entry.pdf)
            by_name[entry.pdf] = entry
            by_patient.setdefault(entry.patient_id, []).append(entry.pdf)
        return cls(
            entries=items,
            _by_name=by_name,
            _by_patient={pid: tuple(sorted(names)) for pid, names in by_patient.items()},
        )

    # --- I/O ----------------------------------------------------------------

    def write(self, pdfs_dir: Path) -> Path:
        """Atomically write ``render_index.json`` into the PDF directory.

        Temp file + ``os.replace`` (RULES.md 14) — a crash mid-write never
        leaves a partial index."""

        target = pdfs_dir / INDEX_FILENAME
        payload: dict[str, Any] = {
            "version": _SCHEMA_VERSION,
            "entries": [
                {"pdf": e.pdf, "patient_id": e.patient_id, "encounter_id": e.encounter_id}
                for e in self.entries
            ],
        }
        # PHI-BY-DESIGN: the sidecar maps PDF filenames (which embed patient
        # name + date of service) to their owning patient/encounter ids; same
        # secure_output_dir hardening as write_fhir_bundle (_shared.py); see
        # SECURITY.md.
        # codeql[py/clear-text-storage-sensitive-data]
        atomic_write_text(target, json.dumps(payload, indent=2, sort_keys=True))
        return target

    @classmethod
    def load(cls, pdfs_dir: Path | None) -> RenderIndex | None:
        """Load the index from ``pdfs_dir/render_index.json``.

        Returns ``None`` on any missing/unreadable/malformed file — fail
        closed, never guess from names. Logs a WARNING on malformed input."""
        if pdfs_dir is None or not pdfs_dir.is_dir():
            return None
        path = pdfs_dir / INDEX_FILENAME
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Basename only; the full output-tree path stays out of logs (RULES.md 2).
            logger.warning("render index unreadable at %s (%s)", INDEX_FILENAME, exc_tag(exc))
            return None
        if not isinstance(raw, dict) or raw.get("version") != _SCHEMA_VERSION:
            logger.warning(
                "render index schema mismatch at %s (expected version %s)",
                INDEX_FILENAME,
                _SCHEMA_VERSION,
            )
            return None
        entries_raw = raw.get("entries")
        if not isinstance(entries_raw, list):
            logger.warning("render index has no entries list at %s", INDEX_FILENAME)
            return None
        entries: list[RenderEntry] = []
        for item in entries_raw:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("pdf"), str)
                or not isinstance(item.get("patient_id"), str)
                or not isinstance(item.get("encounter_id"), str)
            ):
                logger.warning("render index has malformed entry at %s; skipping", INDEX_FILENAME)
                continue
            entries.append(
                RenderEntry(
                    pdf=item["pdf"],
                    patient_id=item["patient_id"],
                    encounter_id=item["encounter_id"],
                )
            )
        try:
            return cls.from_entries(entries)
        except RenderIndexConflict as exc:
            # A self-conflicting index is untrustworthy for attribution; treat
            # it like a corrupt one and fail closed to unattributed/.
            logger.warning("render index self-conflicts at %s (%s)", INDEX_FILENAME, exc_tag(exc))
            return None

    # --- queries ------------------------------------------------------------

    def for_patient(self, patient_id: str) -> tuple[str, ...]:
        """PDF filenames owned by ``patient_id`` (empty when none)."""
        return self._by_patient.get(patient_id, ())

    def lookup(self, pdf_name: str) -> RenderEntry | None:
        """The entry for a PDF filename, or ``None`` if not indexed."""
        return self._by_name.get(pdf_name)
