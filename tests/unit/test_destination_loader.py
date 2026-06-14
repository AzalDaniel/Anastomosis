"""Tests for defensive browser-pack discovery (precedence, overlay, diagnosis).

The user directory is monkeypatched to a tmp path so no test ever touches a real
``~/.anastomosis``. Synthetic selectors only — they are CSS strings, never PHI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import anastomosis.destinations.loader as loader
from anastomosis.destinations.browserpack import PackNotReadyError
from anastomosis.destinations.loader import (
    BrowserPackError,
    load_destination_pack,
)

# A complete, ready selector set (every required slot a real CSS string).
_READY_SELECTORS = {
    "patient_search_input": "#search",
    "patient_search_submit": "#go",
    "patient_result_row": ".result-row",
    "patient_banner_name": "#banner-name",
    "patient_banner_dob": "#banner-dob",
    "documents_list_item": ".doc-item",
    "upload_file_input": "#file",
    "upload_submit": "#submit",
    "upload_success_marker": ".success",
}


def _selectors_yaml(selectors: dict[str, str]) -> str:
    lines = ["selectors:"]
    lines += [f'  {k}: "{v}"' for k, v in selectors.items()]
    return "\n".join(lines) + "\n"


def _user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the loader's user directory at an isolated tmp path."""
    udir = tmp_path / "user_destinations"
    monkeypatch.setattr(loader, "user_destinations_dir", lambda: udir)
    return udir


def _write_pack(root: Path, name: str, *, selectors: dict[str, str] | str) -> Path:
    pack = root / name
    pack.mkdir(parents=True, exist_ok=True)
    if isinstance(selectors, str):
        sel_block = selectors
    else:
        sel_block = "\n".join(f'  {k}: "{v}"' for k, v in selectors.items())
    (pack / "pack.yaml").write_text(
        f"name: {name}\ndisplay: {name}\nconfig:\n  search_by: both\nselectors:\n{sel_block}\n",
        encoding="utf-8",
    )
    return pack


# --- built-in tebra ---


def test_builtin_tebra_loads_as_not_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _user_dir(tmp_path, monkeypatch)  # empty user dir -> no overlay
    loaded = load_destination_pack("tebra")
    assert loaded.name == "tebra"
    assert loaded.builtin is True
    assert loaded.ready is False
    assert loaded.config.patient_search_url is None
    assert loaded.config.search_by == "both"
    with pytest.raises(PackNotReadyError) as excinfo:
        loaded.require_selectors()
    assert "anast destination init" in str(excinfo.value)


def test_user_overlay_makes_tebra_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    udir = _user_dir(tmp_path, monkeypatch)
    (udir / "tebra").mkdir(parents=True)
    (udir / "tebra" / "selectors.yaml").write_text(
        _selectors_yaml(_READY_SELECTORS), encoding="utf-8"
    )
    loaded = load_destination_pack("tebra")
    assert loaded.builtin is True  # manifest still the built-in scaffold
    assert loaded.ready is True
    sel = loaded.require_selectors()
    assert sel.patient_search_input == "#search"
    assert loaded.selectors_source == udir / "tebra" / "selectors.yaml"


# --- precedence: --pack-dir > user dir > builtin ---


def test_pack_dir_beats_user_and_builtin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    udir = _user_dir(tmp_path, monkeypatch)
    # A user overlay for tebra exists (would make the built-in ready)...
    (udir / "tebra").mkdir(parents=True)
    (udir / "tebra" / "selectors.yaml").write_text(
        _selectors_yaml(_READY_SELECTORS), encoding="utf-8"
    )
    # ...but an explicit --pack-dir tebra has a DISTINCT search input selector.
    pack_root = tmp_path / "packs"
    distinct = dict(_READY_SELECTORS, patient_search_input="#PACK_DIR_WINS")
    _write_pack(pack_root, "tebra", selectors=distinct)
    loaded = load_destination_pack("tebra", [pack_root])
    assert loaded.builtin is False
    assert loaded.require_selectors().patient_search_input == "#PACK_DIR_WINS"


def test_user_dir_beats_builtin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    udir = _user_dir(tmp_path, monkeypatch)
    (udir / "tebra").mkdir(parents=True)
    (udir / "tebra" / "selectors.yaml").write_text(
        _selectors_yaml(_READY_SELECTORS), encoding="utf-8"
    )
    # No --pack-dir: the built-in manifest + user overlay combine to ready.
    loaded = load_destination_pack("tebra")
    assert loaded.builtin is True
    assert loaded.ready is True


def test_pack_dir_carries_its_own_selectors_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user_dir(tmp_path, monkeypatch)
    pack_root = tmp_path / "packs"
    discover = dict.fromkeys(_READY_SELECTORS, "DISCOVER me")
    pack = _write_pack(pack_root, "acme", selectors=discover)
    # A selectors.yaml beside the pack overlays its DISCOVER placeholders.
    (pack / "selectors.yaml").write_text(_selectors_yaml(_READY_SELECTORS), encoding="utf-8")
    loaded = load_destination_pack("acme", [pack_root])
    assert loaded.ready is True
    assert loaded.require_selectors().upload_submit == "#submit"


# --- unknown / broken ---


def test_unknown_pack_raises_naming_search_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user_dir(tmp_path, monkeypatch)
    with pytest.raises(BrowserPackError, match="no destination pack 'ghost'"):
        load_destination_pack("ghost")


def test_broken_yaml_is_diagnosed_with_the_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user_dir(tmp_path, monkeypatch)
    pack_root = tmp_path / "packs"
    pack = pack_root / "acme"
    pack.mkdir(parents=True)
    broken = pack / "pack.yaml"
    broken.write_text("name: acme\nconfig: {oops: [unterminated\n", encoding="utf-8")
    with pytest.raises(BrowserPackError) as excinfo:
        load_destination_pack("acme", [pack_root])
    # The diagnosis names the offending file so the operator knows what to fix.
    assert str(broken) in str(excinfo.value)


def test_unknown_config_key_is_diagnosed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _user_dir(tmp_path, monkeypatch)
    pack_root = tmp_path / "packs"
    pack = pack_root / "acme"
    pack.mkdir(parents=True)
    (pack / "pack.yaml").write_text(
        "name: acme\nconfig:\n  bogus_key: 1\nselectors:\n  patient_search_input: '#x'\n",
        encoding="utf-8",
    )
    with pytest.raises(BrowserPackError, match="config"):
        load_destination_pack("acme", [pack_root])
