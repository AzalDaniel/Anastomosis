"""Direct tests for the one file/byte digest seam (rule 16)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from anastomosis.core.hashutil import HASH_CHUNK_BYTES, file_sha256, hash_and_size, sha256_hex

_KNOWN = b"anastomosis synthetic fixture bytes\n"
_KNOWN_SHA = "d0b28aa96f5225114f75efa9c638e9f4d77d97e6956b0a48960f4a231f542784"
#: Longer than one read, so the streaming loop runs more than once.
_STREAMED = b"anastomosis" * 200_000


def test_hash_and_size_returns_the_known_digest_and_byte_count(tmp_path: Path) -> None:
    target = tmp_path / "sample.bin"
    target.write_bytes(_KNOWN)
    assert hash_and_size(target) == (_KNOWN_SHA, len(_KNOWN))


def test_hash_and_size_streams_a_file_past_one_chunk(tmp_path: Path) -> None:
    assert len(_STREAMED) > HASH_CHUNK_BYTES
    target = tmp_path / "streamed.bin"
    target.write_bytes(_STREAMED)
    assert hash_and_size(target) == (hashlib.sha256(_STREAMED).hexdigest(), len(_STREAMED))


def test_hash_and_size_raises_for_a_path_it_cannot_read(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        hash_and_size(tmp_path / "absent.bin")


def test_file_sha256_is_the_same_digest_without_the_size(tmp_path: Path) -> None:
    target = tmp_path / "sample.bin"
    target.write_bytes(_KNOWN)
    assert file_sha256(target) == _KNOWN_SHA


def test_file_sha256_re_raises_when_no_sentinel_is_named(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        file_sha256(tmp_path / "absent.bin")


@pytest.mark.parametrize("sentinel", [None, "unreadable", 0, False])
def test_file_sha256_answers_with_each_sentinel_form(tmp_path: Path, sentinel: object) -> None:
    assert file_sha256(tmp_path / "absent.bin", unreadable=sentinel) is sentinel


def test_file_sha256_treats_a_directory_as_unreadable(tmp_path: Path) -> None:
    assert file_sha256(tmp_path, unreadable=None) is None


def test_sha256_hex_digests_its_parts_as_one_stream() -> None:
    assert sha256_hex(b"alpha", b"beta") == hashlib.sha256(b"alphabeta").hexdigest()


def test_sha256_hex_of_nothing_is_the_empty_digest() -> None:
    assert sha256_hex() == hashlib.sha256(b"").hexdigest()
