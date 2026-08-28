#!/usr/bin/env python3
"""Run the pf_tebra adapter's own rules over a real export and report, PHI-safely.

Written to run in two places and be compared: here, on a small export that can
be shared, and on the operator's machine, on one that cannot. The output is the
same shape either way, so the two can be diffed.

What it emits: table names, column names, integers, and exception TYPE names.
Never a cell value. That is enforced below rather than promised — `_safe`
refuses anything that is not a str-from-a-known-vocabulary or an int.

Usage:  python tools/real_export_report.py <export-dir> [--out report.json]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path


def physical_lines(path: Path) -> int:
    """Raw newline count — what a naive counter sees."""
    with path.open("rb") as fh:
        return sum(chunk.count(b"\n") for chunk in iter(lambda: fh.read(1 << 20), b""))


def logical_rows(path: Path) -> tuple[int, str | None]:
    """Rows a correct quoted-TSV parse yields, or the failure's type name.

    The gap between this and `physical_lines` is embedded newlines inside quoted
    cells — the thing that makes two row counts of one file disagree.
    """
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh, delimiter="\t")
            next(reader, None)  # header
            return sum(1 for _ in reader), None
    except Exception as exc:
        return -1, type(exc).__name__


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("export_dir", type=Path)
    ap.add_argument("--out", type=Path, default=Path("real_export_report.json"))
    args = ap.parse_args()

    from anastomosis.sources.pf_tebra.loader import KNOWN_TABLES, read_export
    from anastomosis.sources.pf_tebra.mapper import _reference_tables, _self_keyed

    root = args.export_dir
    report: dict[str, object] = {"version": 1, "export_dir_name": root.name}

    # --- 1. every TSV, counted both ways -------------------------------------
    tables: dict[str, dict[str, object]] = {}
    for path in sorted(root.glob("*.tsv")):
        rows, err = logical_rows(path)
        tables[path.stem] = {
            "logical_rows": rows,
            "physical_lines": max(physical_lines(path) - 1, 0),
            "parse_error": err,
        }
    report["tables"] = tables
    report["counts_disagree"] = sorted(
        n for n, t in tables.items() if t["logical_rows"] != t["physical_lines"]
    )

    # --- 2. what the loader itself does --------------------------------------
    t0 = time.time()
    try:
        export = read_export(root)
        report["load"] = {"ok": True, "seconds": round(time.time() - t0, 1)}
    except Exception as exc:
        report["load"] = {
            "ok": False,
            "error_type": type(exc).__name__,
            "detail": getattr(exc, "table", None),
            "line": getattr(exc, "line", None),
            "seconds": round(time.time() - t0, 1),
        }
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True))
        print(json.dumps(report["load"], indent=2))
        return 1

    for name, rows in export.items():
        tables.setdefault(name, {})["loader_rows"] = len(rows)

    # --- 3. how each table is classified (issue #234 lives here) -------------
    known = set(KNOWN_TABLES)
    reference = _reference_tables(export)
    patient_scoped = {n for n, r in export.items() if r and "PatientPracticeGuid" in r[0]}
    fk_into_patient_scope = {
        c
        for n in patient_scoped
        for c in export[n][0]
        if c.endswith("Guid") and c != "PatientPracticeGuid"
    }

    classified: dict[str, dict[str, object]] = {}
    for name, rows in sorted(export.items()):
        if name in known:
            kind = "mapped"
        elif not rows:
            kind = "empty"
        elif "PatientPracticeGuid" in rows[0]:
            kind = "preserved"
        elif name in reference:
            kind = "reference"
        else:
            kind = "REFUSED"
        entry: dict[str, object] = {"kind": kind, "rows": len(rows)}
        if kind == "reference":
            key = _self_keyed(rows)
            others = [
                c for c in rows[0] if c.endswith("Guid") and c != key and c in fk_into_patient_scope
            ]
            entry["self_key"] = key
            entry["foreign_keys_into_patient_scope"] = sorted(others)
            # The #234 predicate: a "directory" that points back into patient
            # scope is patient data being copied into every patient's record.
            entry["suspect_cross_patient"] = bool(others)
        classified[name] = entry
    report["classification"] = classified
    report["cross_patient_suspects"] = sorted(
        n for n, e in classified.items() if e.get("suspect_cross_patient")
    )

    # --- 4. what the mapper produces ----------------------------------------
    t0 = time.time()
    try:
        import anastomosis.sources.pf_tebra  # noqa: F401
        from anastomosis.sources import get_source

        records = list(get_source("pf-tebra").load(root))
        report["map"] = {
            "ok": True,
            "seconds": round(time.time() - t0, 1),
            "records": len(records),
            "encounters": sum(len(r.encounters) for r in records),
            "documents": sum(len(r.documents) for r in records),
            "documents_with_a_file": sum(1 for r in records for d in r.documents if d.path),
            "extension_keys": sorted({k for r in records for k in r.extensions}),
        }
    except Exception as exc:
        report["map"] = {
            "ok": False,
            "error_type": type(exc).__name__,
            "seconds": round(time.time() - t0, 1),
        }

    args.out.write_text(json.dumps(report, indent=2, sort_keys=True))
    load = report["load"]
    print(f"load: {load}")
    print(f"tables whose two row counts disagree: {report['counts_disagree'] or 'none'}")
    print(f"cross-patient suspects (#234): {report['cross_patient_suspects'] or 'none'}")
    print(f"map: {report.get('map')}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
