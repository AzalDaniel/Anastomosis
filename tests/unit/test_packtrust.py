"""Tests for external-pack hash-pinning + explicit trust.

The security property under test: an external pack's ``context.py`` is NEVER
``exec_module``'d unless the pack is trusted at its current content hash. The
"not executed" assertions use a pack whose ``context.py`` sets a flag on its own
module at import time — if the flag never appears, the code never ran.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import anastomosis.reconstruct.packtrust as packtrust
from anastomosis.reconstruct import discover_packs
from anastomosis.reconstruct.packs import _load_pack_snapshot
from anastomosis.reconstruct.packtrust import PackTrust, pack_content_hash, read_pack_snapshot

# A minimal-but-valid external pack whose context.py sets a flag on its own
# module at IMPORT time, so its execution is observable from outside. A
# file-based sentinel would be refused by restricted pack execution's
# no-filesystem rule before proving anything about the trust gate; a
# module-level assignment needs no capability. The loader registers each
# loaded pack module in ``sys.modules`` under a per-pack
# ``anastomosis._pack_context_*`` name, which is where ``_executed`` looks.
_CONTEXT_PY = "EXECUTED = True\ndef build_context(encounter, record, cfg):\n    return {}\n"

_CONTEXT_MODULE_PREFIX = "anastomosis._pack_context_"
_PACK_YAML = 'name: {name}\nversion: "0.1"\ndescription: trust test pack\n'
_TEMPLATE = "<html><body>{{ anything }}</body></html>\n"


def _make_pack(parent: Path, name: str = "trust_probe") -> Path:
    pack = parent / name
    pack.mkdir(parents=True)
    (pack / "context.py").write_text(_CONTEXT_PY, encoding="utf-8")
    (pack / "pack.yaml").write_text(_PACK_YAML.format(name=name), encoding="utf-8")
    (pack / "template.html").write_text(_TEMPLATE, encoding="utf-8")
    return pack


def _pack_context_modules() -> list[str]:
    return [name for name in list(sys.modules) if name.startswith(_CONTEXT_MODULE_PREFIX)]


def _executed() -> bool:
    """Whether any pack ``context.py`` body has run since the last reset."""
    return any(getattr(sys.modules[name], "EXECUTED", False) for name in _pack_context_modules())


def _reset_executed() -> None:
    """Forget every loaded pack module, so the next load is observed on its own."""
    for name in _pack_context_modules():
        del sys.modules[name]


@pytest.fixture(autouse=True)
def _clean_pack_modules() -> Iterator[None]:
    """A loaded pack module outlives its test; leaking one would make the NEXT
    test's "was it executed?" answer belong to the previous test."""
    _reset_executed()
    yield
    _reset_executed()


# A pack whose build_context returns a marker baked into context.py's source, so
# WHICH bytes executed (original vs. a later on-disk swap) is directly observable.
def _marker_context(marker: str) -> str:
    return (
        f"MARKER = {marker!r}\n"
        "def build_context(encounter, record, cfg):\n"
        '    return {"marker": MARKER}\n'
    )


def _make_marker_pack(parent: Path, marker: str, name: str = "marker_probe") -> Path:
    pack = parent / name
    pack.mkdir(parents=True)
    (pack / "context.py").write_text(_marker_context(marker), encoding="utf-8")
    (pack / "pack.yaml").write_text(_PACK_YAML.format(name=name), encoding="utf-8")
    (pack / "template.html").write_text(_TEMPLATE, encoding="utf-8")
    return pack


class _FakeChromium:
    """Writes a real (tiny) PDF to the given path, so the CLI render path runs."""

    def __init__(self, **kwargs: object) -> None:
        pass

    def render(self, html: str, pdf_path: Path) -> None:
        import pymupdf

        doc = pymupdf.open()
        doc.new_page(width=612, height=792)
        doc.save(str(pdf_path))
        doc.close()

    def close(self) -> None:
        pass


# --- pack_content_hash ---------------------------------------------------------


def test_content_hash_is_stable_and_sensitive(tmp_path: Path) -> None:
    pack = _make_pack(tmp_path)
    h0 = pack_content_hash(pack)
    assert h0 == pack_content_hash(pack)  # stable for unchanged files
    for fname in ("context.py", "template.html", "pack.yaml"):
        before = pack_content_hash(pack)
        original = (pack / fname).read_text(encoding="utf-8")
        (pack / fname).write_text(original + "\n# edit\n", encoding="utf-8")
        assert pack_content_hash(pack) != before, f"hash ignored a change to {fname}"
        (pack / fname).write_text(original, encoding="utf-8")  # restore
        assert pack_content_hash(pack) == before  # back to the original digest


def test_content_hash_matches_documented_byte_layout(tmp_path: Path) -> None:
    # Pins the on-the-wire digest: context.py, template.html, pack.yaml in that
    # order, each prefixed by b"\0<name>\0" then its raw bytes. The snapshot-based
    # implementation must stay byte-identical to this documented layout so
    # already-trusted packs keep their hashes across the refactor.
    pack = _make_pack(tmp_path)
    expected = hashlib.sha256()
    for name in ("context.py", "template.html", "pack.yaml"):
        expected.update(b"\0" + name.encode("utf-8") + b"\0")
        expected.update((pack / name).read_bytes())
    assert pack_content_hash(pack) == expected.hexdigest()


def test_snapshot_hash_equals_pack_content_hash(tmp_path: Path) -> None:
    # The hash the loader gates on (snapshot.content_hash) is the same value
    # pack_content_hash reports — one definition, computed from one read.
    pack = _make_pack(tmp_path)
    snapshot = read_pack_snapshot(pack)
    assert snapshot.content_hash == pack_content_hash(pack)
    assert set(snapshot.files) == {"context.py", "template.html", "pack.yaml"}


def test_snapshot_load_pins_code_against_toctou_swap(tmp_path: Path) -> None:
    # The TOCTOU invariant: the bytes that execute are the bytes that were
    # snapshotted (and therefore hashed). Take the snapshot, then swap context.py
    # on disk (a hostile writer racing the check), then load FROM the snapshot —
    # the ORIGINAL code must run, never the swapped on-disk code.
    pack = _make_marker_pack(tmp_path / "ext", "ORIGINAL")
    snapshot = read_pack_snapshot(pack)
    (pack / "context.py").write_text(_marker_context("SWAPPED"), encoding="utf-8")

    status = _load_pack_snapshot(snapshot, "pack-dir")
    assert status.pack is not None
    assert status.pack.build_context(None, None, None)["marker"] == "ORIGINAL"


# --- PackTrust store -----------------------------------------------------------


def test_trust_store_round_trips_and_pins_hash(tmp_path: Path) -> None:
    pack = _make_pack(tmp_path / "p")
    store = PackTrust(tmp_path / "trust.json")
    h = pack_content_hash(pack)
    assert not store.is_trusted(pack, h)  # nothing trusted yet
    store.record(pack, h)
    assert store.is_trusted(pack, h)
    # Reloading from disk preserves the trust.
    assert PackTrust(tmp_path / "trust.json").is_trusted(pack, h)
    # A different hash (changed code) is not trusted.
    assert not store.is_trusted(pack, h + "00")


def test_trust_store_tolerates_missing_and_garbage(tmp_path: Path) -> None:
    assert PackTrust(tmp_path / "absent.json")._store == {}
    garbage = tmp_path / "garbage.json"
    garbage.write_text("not json{", encoding="utf-8")
    assert PackTrust(garbage)._store == {}  # garbage → trusts nothing, never raises


def test_concurrent_record_merges_entries(tmp_path: Path) -> None:
    # Lost-update regression: two PackTrust instances, each constructed against
    # the (empty) store so neither's ctor snapshot sees the other's entry, record
    # DIFFERENT packs. record() re-reads under the lock and merges, so the second
    # write must NOT clobber the first — both entries survive on disk.
    pack_a = _make_pack(tmp_path / "a", "pack_a")
    pack_b = _make_pack(tmp_path / "b", "pack_b")
    store_path = tmp_path / "state" / "trust.json"

    trust_a = PackTrust(store_path)
    trust_b = PackTrust(store_path)
    hash_a = pack_content_hash(pack_a)
    hash_b = pack_content_hash(pack_b)

    trust_a.record(pack_a, hash_a)
    trust_b.record(pack_b, hash_b)  # ctor snapshot predates trust_a's write

    on_disk = PackTrust(store_path)
    assert on_disk.is_trusted(pack_a, hash_a)
    assert on_disk.is_trusted(pack_b, hash_b)
    # The merged view is also reflected in the recorder's own store.
    assert trust_b.is_trusted(pack_a, hash_a)


def test_record_leaves_parseable_json(tmp_path: Path) -> None:
    # Atomicity smoke: after record(), the store is a complete, parseable JSON
    # object (never a torn half-write) — the temp-file + os.replace guarantee.
    pack = _make_pack(tmp_path / "p")
    store_path = tmp_path / "state" / "trust.json"
    PackTrust(store_path).record(pack, pack_content_hash(pack))
    data = json.loads(store_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data[str(pack.resolve())] == pack_content_hash(pack)


# --- discover_packs enforcement ------------------------------------------------


def test_untrusted_external_pack_is_refused_and_not_executed(tmp_path: Path) -> None:
    pack = _make_pack(tmp_path / "ext")
    store = PackTrust(tmp_path / "trust.json")
    statuses = discover_packs([pack.parent], allow_external=True, trust=store)
    status = statuses["trust_probe"]
    assert status.pack is None
    assert "untrusted" in (status.diagnosis or "")
    assert not _executed(), "untrusted context.py must NOT be exec'd"


def test_trust_new_records_then_loads_and_changes_re_refuse(tmp_path: Path) -> None:
    pack = _make_pack(tmp_path / "ext")
    store_path = tmp_path / "trust.json"

    # First use with trust_new: records the hash and loads (code runs once).
    statuses = discover_packs(
        [pack.parent], allow_external=True, trust=PackTrust(store_path), trust_new=True
    )
    assert statuses["trust_probe"].pack is not None
    assert _executed()

    # A later run WITHOUT trust_new still loads — the hash is trusted now.
    _reset_executed()
    statuses = discover_packs([pack.parent], allow_external=True, trust=PackTrust(store_path))
    assert statuses["trust_probe"].pack is not None
    assert _executed()

    # Mutating context.py un-trusts it: refused again, not executed.
    _reset_executed()
    (pack / "context.py").write_text(_CONTEXT_PY + "# changed\n", encoding="utf-8")
    statuses = discover_packs([pack.parent], allow_external=True, trust=PackTrust(store_path))
    assert statuses["trust_probe"].pack is None
    assert not _executed(), "changed (un-trusted) context.py must NOT be exec'd"


def test_trust_none_preserves_consent_only_behavior(tmp_path: Path) -> None:
    """With no trust store, allow_external loads external packs as before
    (backwards-compatible with packgen/emit and existing callers)."""
    pack = _make_pack(tmp_path / "ext")
    statuses = discover_packs([pack.parent], allow_external=True)  # trust=None
    assert statuses["trust_probe"].pack is not None
    assert _executed()


def test_builtin_packs_load_without_trust(tmp_path: Path) -> None:
    store = PackTrust(tmp_path / "trust.json")  # empty store
    statuses = discover_packs(trust=store)  # builtins only; never hash-gated
    assert statuses["generic_soap"].pack is not None
    assert statuses["practice_fusion_soap"].pack is not None


# --- CLI integration -----------------------------------------------------------


def test_cli_refuses_untrusted_pack_dir_then_trusts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("pymupdf", reason="render path needs PyMuPDF")
    from typer.testing import CliRunner

    import anastomosis.reconstruct.chromium as chromium
    from anastomosis.cli import app

    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    # Redirect the trust store off the real ~/.anastomosis.
    monkeypatch.setattr(packtrust, "user_pack_trust_path", lambda: tmp_path / "trust.json")

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"
    pack = _make_pack(tmp_path / "packs")
    runner = CliRunner()
    base = [
        "pipeline",
        "run",
        str(fixture),
        "--out",
        str(tmp_path / "out"),
        "--pack-dir",
        str(pack.parent),
        "--pack",
        "trust_probe",
        "--no-qa",
    ]

    refused = runner.invoke(app, base)
    assert refused.exit_code == 2, refused.output
    assert "unavailable" in refused.output
    assert not _executed(), "untrusted pack must not run via the CLI either"

    trusted = runner.invoke(app, [*base, "--trust-pack"])
    assert trusted.exit_code == 0, trusted.output
    assert _executed()
