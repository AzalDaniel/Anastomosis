"""FHIR R4 / US Core ingest adapter (the universal "any EHR in" lane).

Reads the structured export every certified US EHR can produce — a FHIR R4
Bundle (``Patient/$everything``) or a Bulk-Data ``$export`` NDJSON set — into
canonical :class:`PatientRecord` objects. One Patient resource yields one
record; resource types with no canonical home are preserved into ``extensions``
(the lossless guarantee). The per-field mapping lives in ``mapper.py``.

This is deliberately distinct from :func:`anastomosis.core.fhir.ingest.from_bundle`,
which round-trips THIS project's own export (its ``urn:anastomosis:*``
extensions). This adapter targets *arbitrary* US Core exports, so it reads only
the public US Core codings — making "FHIR EHR A → EHR B" a real migration, not a
self-round-trip.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from anastomosis.core.model import PatientRecord
from anastomosis.sources.base import register

from .mapper import records_from_resources

__all__ = ["FHIRR4Adapter"]

# Cheap structural sniff. A FHIR Bundle JSON declares both markers near the top;
# an NDJSON $export file's first line is a JSON resource. Reading a bounded head
# avoids parsing a multi-megabyte export just to answer "is this mine?".
_SNIFF_BYTES = 65536
_BUNDLE_MARKERS = (b'"resourceType"', b'"Bundle"')
_RESOURCE_MARKER = b'"resourceType"'


def _first_line(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.readline(_SNIFF_BYTES)


def _resources_from_file(path: Path) -> list[dict[str, Any]]:
    """Every FHIR resource in one file (Bundle entries, NDJSON lines, or a lone
    resource). Loud on malformed JSON — a corrupt export must not parse to empty."""
    if path.suffix.lower() == ".ndjson":
        resources: list[dict[str, Any]] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line:
                resources.append(json.loads(line))
        return resources
    doc = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(doc, dict) and doc.get("resourceType") == "Bundle":
        return [
            entry["resource"]
            for entry in doc.get("entry", [])
            if isinstance(entry, dict) and isinstance(entry.get("resource"), dict)
        ]
    return [doc] if isinstance(doc, dict) and doc.get("resourceType") else []


class FHIRR4Adapter:
    name = "fhir-r4"
    description = "FHIR R4 / US Core export (Bundle JSON or Bulk-Data $export NDJSON)"

    def detect(self, path: Path) -> bool:
        for json_file in path.glob("*.json"):
            try:
                head = json_file.read_bytes()[:_SNIFF_BYTES]
            except OSError:
                continue
            if all(marker in head for marker in _BUNDLE_MARKERS):
                return True
        for ndjson_file in path.glob("*.ndjson"):
            try:
                if _RESOURCE_MARKER in _first_line(ndjson_file):
                    return True
            except OSError:
                continue
        return False

    def load(self, path: Path) -> Iterator[PatientRecord]:
        """Parse every Bundle/NDJSON file under ``path`` into patient records.

        All resources across all files are pooled before grouping, so a
        ``$export`` split into ``Patient.ndjson`` / ``Observation.ndjson`` / …
        joins correctly. Files are read in sorted order for determinism; a
        deterministic ``source_file`` is recorded in provenance.
        """
        resources: list[dict[str, Any]] = []
        files = sorted(
            [p for p in path.glob("*.json") if self._looks_fhir_json(p)]
            + list(path.glob("*.ndjson"))
        )
        for data_file in files:
            resources.extend(_resources_from_file(data_file))
        if not resources:
            return
        source_file = files[0].name if len(files) == 1 else None
        yield from records_from_resources(resources, source_file=source_file)

    @staticmethod
    def _looks_fhir_json(path: Path) -> bool:
        """A ``.json`` worth parsing: a Bundle or a single resource (has the
        resourceType marker). A non-FHIR ``.json`` in the dir is left untouched."""
        try:
            head = path.read_bytes()[:_SNIFF_BYTES]
        except OSError:
            return False
        return _RESOURCE_MARKER in head


register(FHIRR4Adapter())
