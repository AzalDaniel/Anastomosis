"""Persisted index of (pdf_filename → patient_id, encounter_id).

The reconstruction engine names each chart ``{family}_{given}_{dos}_{type}.pdf``
and a patient id never appears IN the filename. Previously the archive and
bundle deliverers reverse-inferred ownership from the leading
``{family}_{given}_`` prefix; that quietly cross-attributes any two patients
sharing both names (a synthetic-fixture collision today, a real-world hazard
the moment two Smith Johns enter the same export). The producer — the
engine — already knows the truth: every ``RenderedDoc`` carries
``patient_id`` and ``encounter_id``. The deliverers just never received it.

This module is the sidecar that closes that gap. The engine writes
``render_index.json`` into the same directory as the PDFs at the end of a
run; the archive and bundle deliverers ``load`` it before doing any
attribution. PDFs without an index entry are kept (never silently dropped)
but placed into an ``unattributed/`` slot — never guessed onto a patient.

JSON shape (sorted for deterministic byte output)::

    {
      "version": 1,
      "entries": [
        {"pdf": "Smith_John_05-10-2023_SOAP.pdf",
         "patient_id": "feedface-…-0001",
         "encounter_id": "feedface-e000-…"},
        …
      ]
    }
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anastomosis.core.logutil import exc_tag

__all__ = ["INDEX_FILENAME", "RenderEntry", "RenderIndex"]

logger = logging.getLogger(__name__)

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

    Lookups are O(1) — ``_by_name`` and ``_by_patient`` are built once at
    construction and frozen with the dataclass. The class is a plain,
    immutable container.
    """

    entries: tuple[RenderEntry, ...]
    _by_name: dict[str, RenderEntry]
    _by_patient: dict[str, tuple[str, ...]]

    @classmethod
    def from_entries(cls, entries: Iterable[RenderEntry]) -> RenderIndex:
        items = tuple(sorted(entries, key=lambda e: e.pdf))
        by_name: dict[str, RenderEntry] = {}
        by_patient: dict[str, list[str]] = {}
        for entry in items:
            # Two engine runs into the same dir + a name clash would be a
            # bug upstream; we keep the LAST entry deterministically so
            # the index reflects the run that wrote it last. The engine
            # already collision-suffixes within a run (engine.py:138).
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

        Writes to a temp file then ``os.replace``s so a crash mid-write
        never leaves a partial index — same atomicity discipline the
        engine uses for the PDFs themselves (engine.py:210-226).
        """
        import os

        target = pdfs_dir / INDEX_FILENAME
        payload: dict[str, Any] = {
            "version": _SCHEMA_VERSION,
            "entries": [
                {"pdf": e.pdf, "patient_id": e.patient_id, "encounter_id": e.encounter_id}
                for e in self.entries
            ],
        }
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            # PHI-BY-DESIGN: the sidecar maps PDF filenames (which embed patient
            # name + date of service) to their owning patient/encounter ids;
            # same secure_output_dir hardening as write_fhir_bundle (_shared.py);
            # see SECURITY.md.
            # codeql[py/clear-text-storage-sensitive-data]
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return target

    @classmethod
    def load(cls, pdfs_dir: Path | None) -> RenderIndex | None:
        """Load the index from ``pdfs_dir/render_index.json``.

        Returns ``None`` when the directory is missing, the file is
        missing, or the file is unreadable/malformed — the deliverers
        treat that as "no index → fail closed", never as "guess from
        names". A WARNING is logged on a malformed file so a corrupted
        index is never silent.
        """
        if pdfs_dir is None or not pdfs_dir.is_dir():
            return None
        path = pdfs_dir / INDEX_FILENAME
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Name the file by its basename only — the full path sits under the
            # output tree, which the log discipline keeps out of log lines.
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
        return cls.from_entries(entries)

    # --- queries ------------------------------------------------------------

    def for_patient(self, patient_id: str) -> tuple[str, ...]:
        """PDF filenames owned by ``patient_id`` (empty when none)."""
        return self._by_patient.get(patient_id, ())

    def lookup(self, pdf_name: str) -> RenderEntry | None:
        """The entry for a PDF filename, or ``None`` if not indexed."""
        return self._by_name.get(pdf_name)
