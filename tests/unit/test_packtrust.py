"""Tests for external-pack hash-pinning + explicit trust.

The security property under test: an external pack's ``context.py`` is NEVER
``exec_module``'d unless the pack is trusted at its current content hash. The
"not executed" assertions use a pack whose ``context.py`` writes a sentinel at
import time — if the sentinel never appears, the code never ran.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import anastomosis.reconstruct.packtrust as packtrust
from anastomosis.reconstruct import discover_packs
from anastomosis.reconstruct.packtrust import PackTrust, pack_content_hash

# A minimal-but-valid external pack whose context.py writes a sentinel next to
# itself at IMPORT time, so its execution is observable.
_CONTEXT_PY = (
    "from pathlib import Path\n"
    '(Path(__file__).parent / "_executed").write_text("ran", encoding="utf-8")\n'
    "def build_context(encounter, record, cfg):\n"
    "    return {}\n"
)
_PACK_YAML = 'name: {name}\nversion: "0.1"\ndescription: trust test pack\n'
_TEMPLATE = "<html><body>{{ anything }}</body></html>\n"


def _make_pack(parent: Path, name: str = "trust_probe") -> Path:
    pack = parent / name
    pack.mkdir(parents=True)
    (pack / "context.py").write_text(_CONTEXT_PY, encoding="utf-8")
    (pack / "pack.yaml").write_text(_PACK_YAML.format(name=name), encoding="utf-8")
    (pack / "template.html").write_text(_TEMPLATE, encoding="utf-8")
    return pack


def _executed(pack: Path) -> bool:
    return (pack / "_executed").is_file()


class _FakeChromium:
    """Writes a real (tiny) PDF to the given path, so the CLI render path runs."""

    def __init__(self, **kwargs: object) -> None:
        pass

    def render(self, html: str, pdf_path: Path) -> None:
        import fitz

        doc = fitz.open()
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


# --- discover_packs enforcement ------------------------------------------------


def test_untrusted_external_pack_is_refused_and_not_executed(tmp_path: Path) -> None:
    pack = _make_pack(tmp_path / "ext")
    store = PackTrust(tmp_path / "trust.json")
    statuses = discover_packs([pack.parent], allow_external=True, trust=store)
    status = statuses["trust_probe"]
    assert status.pack is None
    assert "untrusted" in (status.diagnosis or "")
    assert not _executed(pack), "untrusted context.py must NOT be exec'd"


def test_trust_new_records_then_loads_and_changes_re_refuse(tmp_path: Path) -> None:
    pack = _make_pack(tmp_path / "ext")
    store_path = tmp_path / "trust.json"

    # First use with trust_new: records the hash and loads (code runs once).
    statuses = discover_packs(
        [pack.parent], allow_external=True, trust=PackTrust(store_path), trust_new=True
    )
    assert statuses["trust_probe"].pack is not None
    assert _executed(pack)

    # A later run WITHOUT trust_new still loads — the hash is trusted now.
    (pack / "_executed").unlink()
    statuses = discover_packs([pack.parent], allow_external=True, trust=PackTrust(store_path))
    assert statuses["trust_probe"].pack is not None
    assert _executed(pack)

    # Mutating context.py un-trusts it: refused again, not executed.
    (pack / "_executed").unlink()
    (pack / "context.py").write_text(_CONTEXT_PY + "# changed\n", encoding="utf-8")
    statuses = discover_packs([pack.parent], allow_external=True, trust=PackTrust(store_path))
    assert statuses["trust_probe"].pack is None
    assert not _executed(pack), "changed (un-trusted) context.py must NOT be exec'd"


def test_trust_none_preserves_consent_only_behavior(tmp_path: Path) -> None:
    """With no trust store, allow_external loads external packs as before
    (backwards-compatible with packgen/emit and existing callers)."""
    pack = _make_pack(tmp_path / "ext")
    statuses = discover_packs([pack.parent], allow_external=True)  # trust=None
    assert statuses["trust_probe"].pack is not None
    assert _executed(pack)


def test_builtin_packs_load_without_trust(tmp_path: Path) -> None:
    store = PackTrust(tmp_path / "trust.json")  # empty store
    statuses = discover_packs(trust=store)  # builtins only; never hash-gated
    assert statuses["generic_soap"].pack is not None
    assert statuses["practice_fusion_soap"].pack is not None


# --- CLI integration -----------------------------------------------------------


def test_cli_refuses_untrusted_pack_dir_then_trusts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fitz", reason="render path needs PyMuPDF")
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
    assert not _executed(pack), "untrusted pack must not run via the CLI either"

    trusted = runner.invoke(app, [*base, "--trust-pack"])
    assert trusted.exit_code == 0, trusted.output
    assert _executed(pack)
