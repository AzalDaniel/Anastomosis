"""Manifest + skiplist tests: identity, integrity, and operator exclusion.

Synthetic data only — neutral file names in ``tmp_path``, no patient-derived
values anywhere.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from anastomosis.deliver.browser.manifest import (
    build_manifest,
    is_skiplisted,
    load_skiplist,
)
from anastomosis.destinations.base import UploadItem
from anastomosis.reconstruct.engine import RenderedDoc

# A fixed payload with a known sha256, so item_key/sha/size are asserted
# against an independently computed value rather than the code under test.
_CONTENT = b"anastomosis upload manifest test payload\n"
_SHA = hashlib.sha256(_CONTENT).hexdigest()


def _write(path: Path, content: bytes = _CONTENT) -> Path:
    path.write_bytes(content)
    return path


def test_build_manifest_computes_sha_size_and_item_key(tmp_path: Path) -> None:
    doc_path = _write(tmp_path / "doc-001.pdf")
    docs = [RenderedDoc(path=doc_path, encounter_id="enc-1", patient_id="pat-1")]

    [item] = build_manifest(docs)

    assert item.sha256 == _SHA
    assert item.size_bytes == len(_CONTENT)
    assert item.item_key == f"enc-1:{_SHA[:12]}"
    assert item.encounter_id == "enc-1"
    assert item.patient_id == "pat-1"
    # fingerprint defaults to the file name (UploadItem.__post_init__).
    assert item.fingerprint == "doc-001.pdf"


def test_build_manifest_streams_large_file_consistently(tmp_path: Path) -> None:
    # Bigger than one 1 MiB chunk to exercise the streaming loop.
    blob = b"x" * (1024 * 1024 + 7)
    doc_path = _write(tmp_path / "big.pdf", blob)
    docs = [RenderedDoc(path=doc_path, encounter_id="enc-9", patient_id="pat-9")]

    [item] = build_manifest(docs)

    assert item.sha256 == hashlib.sha256(blob).hexdigest()
    assert item.size_bytes == len(blob)


def test_build_manifest_missing_file_raises(tmp_path: Path) -> None:
    docs = [RenderedDoc(path=tmp_path / "absent.pdf", encounter_id="e", patient_id="p")]
    with pytest.raises(FileNotFoundError):
        build_manifest(docs)


def test_build_manifest_multiple_documents(tmp_path: Path) -> None:
    docs = [
        RenderedDoc(_write(tmp_path / "a.pdf", b"aaa"), "enc-a", "pat-a"),
        RenderedDoc(_write(tmp_path / "b.pdf", b"bbbb"), "enc-b", "pat-b"),
    ]
    items = build_manifest(docs)
    assert [i.encounter_id for i in items] == ["enc-a", "enc-b"]
    assert items[0].size_bytes == 3
    assert items[1].size_bytes == 4


def test_load_skiplist_ignores_blanks_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "skip.txt"
    path.write_text(
        "\n".join(
            [
                "# a comment",
                "  enc-1  ",  # whitespace stripped
                "",
                "   ",  # blank-after-strip
                "enc-2:abc123def456",
                "# trailing comment",
            ]
        ),
        encoding="utf-8",
    )
    skiplist = load_skiplist(path)
    assert skiplist == frozenset({"enc-1", "enc-2:abc123def456"})


def test_load_skiplist_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_skiplist(tmp_path / "nope.txt")


def _item(item_key: str, encounter_id: str) -> UploadItem:
    return UploadItem(
        item_key=item_key,
        encounter_id=encounter_id,
        patient_id="pat-x",
        file_path=Path("doc.pdf"),
        sha256="0" * 64,
        size_bytes=1,
    )


def test_is_skiplisted_matches_item_key() -> None:
    item = _item("enc-1:abc123abc123", "enc-1")
    assert is_skiplisted(item, frozenset({"enc-1:abc123abc123"}))


def test_is_skiplisted_matches_encounter_id() -> None:
    item = _item("enc-1:abc123abc123", "enc-1")
    assert is_skiplisted(item, frozenset({"enc-1"}))


def test_is_skiplisted_no_match() -> None:
    item = _item("enc-1:abc123abc123", "enc-1")
    assert not is_skiplisted(item, frozenset({"enc-2", "enc-3:zzz"}))
