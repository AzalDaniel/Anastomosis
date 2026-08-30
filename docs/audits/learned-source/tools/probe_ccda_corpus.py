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

from pydantic import BaseModel

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


def _populated_fields(model: BaseModel) -> frozenset[str]:
    """The NAMES of the fields this record actually filled — never their values.

    The probe answers "how many documents carried a birth date", not "which
    birth date", so no patient value belongs in its output. That was already
    true by inspection, but only by inspection: the loop bound each value to a
    local in the same scope that built the printed result, and CodeQL rightly
    refused to prove the value could not escape it (a high-severity clear-text
    logging alert on a tool whose whole input is real charts).

    So the invariant becomes structural instead of implied. The value exists
    only inside this function and cannot leave it: the return type carries
    field names, which are schema — the same names in the model source — and a
    caller has nothing else to print even by mistake.
    """
    return frozenset(
        field
        for field in type(model).model_fields
        if getattr(model, field) not in (None, "", [], {})
    )


def _collection_sizes(record: BaseModel) -> dict[str, int]:
    """How many items each clinical collection holds — never the items.

    The same boundary as :func:`_populated_fields`, for the same reason: the
    probe reports "146,015 observations", never an observation. Binding the
    list in the caller put a patient's clinical objects in the scope that
    builds the printed result; here they cannot leave the comprehension.
    """
    return {field: len(getattr(record, field)) for field in COLLECTION_FIELDS}


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
                    if record.patient.id:  # tested, never carried into a count
                        counts["records_with_patient_id"] += 1
                    for field in _populated_fields(record.patient):
                        patient_field_presence[field] += 1
                    for field, size in _collection_sizes(record).items():
                        collection_totals[field] += size
                    for encounter in record.encounters:
                        for field in _populated_fields(encounter):
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
    # PHI-FREE-BY-CONSTRUCTION: `result` holds integers and the field NAMES
    # declared on the models — the same strings that appear in core/model.py —
    # and nothing else; :func:`_populated_fields` and :func:`_collection_sizes`
    # are the boundaries that make that structural rather than merely true.
    # CodeQL still flags this line because a patient record crosses into those
    # helpers and no scanner can see that only names come back, so the
    # suppression is paired with a test that fails if a value ever does:
    # tests/unit/test_corpus_probe_emits_no_values.py.
    # codeql[py/clear-text-logging-sensitive-data]
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if counts["parsed"] == counts["cda_candidates"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
