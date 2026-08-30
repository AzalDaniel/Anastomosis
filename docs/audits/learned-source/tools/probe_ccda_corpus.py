"""PHI-free aggregate probe for C-CDA XML members inside ZIP archives.

The probe never prints archive/member names, XML text, identifiers, or exception
messages. A single fixed scratch filename is reused so patient-derived archive
names cannot escape into diagnostics. Output is aggregate JSON only.
"""

from __future__ import annotations

import argparse
import json
import zipfile
from collections import Counter
from pathlib import Path

from anastomosis.sources.ccda import _looks_like_cda
from anastomosis.sources.ccda.parser import parse_document

MAX_XML_BYTES = 64 * 1024 * 1024
SNIFF_BYTES = 4096
COLLECTION_FIELDS = (
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
    "health_concerns",
    "screening_events",
    "coverages",
    "documents",
    "practitioners",
    "facilities",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus", type=Path)
    parser.add_argument("scratch", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    args.scratch.mkdir(parents=True, exist_ok=True)
    candidate = args.scratch / "candidate.xml"

    archives = sorted(args.corpus.glob("*.zip"))
    counts: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    collection_totals: Counter[str] = Counter()
    patient_field_presence: Counter[str] = Counter()
    encounter_field_presence: Counter[str] = Counter()

    for archive in archives:
        counts["archives"] += 1
        try:
            with zipfile.ZipFile(archive) as bundle:
                members = sorted(bundle.infolist(), key=lambda item: item.filename)
                for member in members:
                    if member.is_dir() or not member.filename.lower().endswith(".xml"):
                        continue
                    counts["xml_members"] += 1
                    if member.file_size > MAX_XML_BYTES:
                        counts["oversized_xml"] += 1
                        continue
                    try:
                        with bundle.open(member) as stream:
                            head = stream.read(SNIFF_BYTES)
                            if not _looks_like_cda(head):
                                counts["non_cda_xml"] += 1
                                continue
                            remainder = stream.read(MAX_XML_BYTES + 1 - len(head))
                    except Exception as exc:  # structural archive failure; type only
                        failures[type(exc).__name__] += 1
                        continue
                    payload = head + remainder
                    if len(payload) > MAX_XML_BYTES:
                        counts["oversized_xml"] += 1
                        continue
                    counts["cda_candidates"] += 1
                    try:
                        candidate.write_bytes(payload)
                        record = parse_document(candidate)
                    except Exception as exc:  # parser failure; never print message
                        failures[type(exc).__name__] += 1
                        continue
                    counts["parsed"] += 1
                    counts["records_with_patient_id"] += int(bool(record.patient.id))
                    for field in record.patient.__class__.model_fields:
                        value = getattr(record.patient, field)
                        if value not in (None, "", [], {}):
                            patient_field_presence[field] += 1
                    for field in COLLECTION_FIELDS:
                        collection = getattr(record, field)
                        collection_totals[field] += len(collection)
                    for encounter in record.encounters:
                        for field in encounter.__class__.model_fields:
                            value = getattr(encounter, field)
                            if value not in (None, "", [], {}):
                                encounter_field_presence[field] += 1
        except Exception as exc:  # invalid archive; type only, never archive name
            failures[f"archive/{type(exc).__name__}"] += 1

    if candidate.exists():
        candidate.unlink()
    result = {
        "limits": {"max_xml_bytes": MAX_XML_BYTES},
        "counts": dict(sorted(counts.items())),
        "failure_types": dict(sorted(failures.items())),
        "collection_totals": dict(sorted(collection_totals.items())),
        "patient_field_presence": dict(sorted(patient_field_presence.items())),
        "encounter_field_presence": dict(sorted(encounter_field_presence.items())),
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if counts["parsed"] == counts["cda_candidates"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
