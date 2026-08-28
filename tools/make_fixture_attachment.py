"""Regenerate the pf_tebra_v9 fixture's one synthetic attachment.

The fixture's `patient-documents.tsv` row names a file by its
`DocumentStorageGuid`; this writes that file so the loader's blob resolution
has something to find. Kept as a script rather than a committed-once blob so
the PDF's provenance is checkable — a reviewer can run it and compare digests
against the entry in `tools/phi_allowlist.txt`.

Synthetic throughout: two lines of fixture prose, no patient data. Run from the
repository root:

    python tools/make_fixture_attachment.py
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

TARGET = Path("tests/fixtures/pf_tebra_v9/binary-content")
STORAGE_GUID = "feedface-d0c0-0000-0000-000000000001"
#: A fixed timestamp, so the output is byte-reproducible (see save() below).
_STAMP = "D:20200101000000Z"
PAGES = (
    "Cardiology referral letter (synthetic fixture).",
    "Page two: continuation of the synthetic referral.",
)


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    out = TARGET / f"{STORAGE_GUID}.pdf"
    doc = pymupdf.open()
    for number, body in enumerate(PAGES, start=1):
        page = doc.new_page()
        page.insert_text((72, 96), body, fontsize=11)
        page.insert_text(
            (72, 120), f"Page {number} of {len(PAGES)} - no real patient data.", fontsize=9
        )
    # PyMuPDF stamps a creation time by default, which would make every run
    # produce a different digest and the allowlist entry unverifiable. Pin the
    # metadata so re-running this reproduces the committed file byte for byte.
    doc.set_metadata({"creationDate": _STAMP, "modDate": _STAMP, "producer": "", "creator": ""})
    doc.del_xml_metadata()
    doc.save(out, garbage=4, deflate=True)
    doc.close()
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
