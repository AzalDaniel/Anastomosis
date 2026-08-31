"""What a non-built-in pack's ``context.py`` is handed when it runs.

Before :mod:`anastomosis.reconstruct.packexec`, a pack that arrived by email and
was pointed at with ``--pack-dir`` executed with the desktop user's full
authority: it could read any file the operator could read, write one anywhere,
open a socket, and spawn a process — all at import, before a single chart was
rendered. Nothing said so, and nothing had chosen it.

These tests are written as hostile packs (synthetic, under ``tmp_path``) that
try exactly those things, and assert both halves: the pack comes back
unavailable with a diagnosis naming what it asked for, AND the side effect it
was reaching for never happened.

What is deliberately NOT asserted here is that a determined attacker is
contained — a restricted globals mapping is not a CPython sandbox, and
``packexec``'s own docstring says so. The property under test is that the
obvious capabilities are gone and their absence is loud.
"""

from __future__ import annotations

import builtins
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from anastomosis.reconstruct import discover_packs
from anastomosis.reconstruct.packexec import PACK_ALLOWED_BUILTINS, PACK_ALLOWED_MODULES
from anastomosis.reconstruct.packs import ORIGIN_PACK_DIR
from anastomosis.reconstruct.packtrust import PackTrust

_PACK_YAML = 'name: {name}\nversion: "0.1"\ndescription: pack-execution probe\n'
_TEMPLATE = "<html><body>{{ anything }}</body></html>\n"
_BUILD = "def build_context(encounter, record, cfg):\n    return {}\n"

_CONTEXT_MODULE_PREFIX = "anastomosis._pack_context_"


@pytest.fixture(autouse=True)
def _clean_pack_modules() -> Iterator[None]:
    """A loaded pack module outlives its test; a leaked one would answer the
    next test's question with the previous test's pack."""
    yield
    for name in [n for n in list(sys.modules) if n.startswith(_CONTEXT_MODULE_PREFIX)]:
        del sys.modules[name]


def _make_pack(parent: Path, body: str, *, name: str = "probe_pack") -> Path:
    pack = parent / name
    pack.mkdir(parents=True)
    (pack / "context.py").write_text(body, encoding="utf-8")
    (pack / "pack.yaml").write_text(_PACK_YAML.format(name=name), encoding="utf-8")
    (pack / "template.html").write_text(_TEMPLATE, encoding="utf-8")
    return pack


def _load(tmp_path: Path, body: str, *, name: str = "probe_pack") -> tuple[object, str]:
    """Discover one external pack; return ``(pack_or_None, diagnosis)``."""
    pack = _make_pack(tmp_path / "ext", body, name=name)
    statuses = discover_packs(
        [pack.parent],
        allow_external=True,
        trust=PackTrust(tmp_path / "trust.json"),
        trust_new=True,
        include_user=False,
    )
    status = statuses[name]
    assert status.origin == ORIGIN_PACK_DIR
    return status.pack, status.diagnosis or ""


# --- the three capabilities the issue names ----------------------------------


def test_a_pack_that_reads_a_file_is_refused(tmp_path: Path) -> None:
    """A layout has no business opening the operator's filesystem."""
    secret = tmp_path / "not_yours.txt"
    secret.write_text("synthetic", encoding="utf-8")
    stolen = tmp_path / "stolen.txt"

    pack, diagnosis = _load(
        tmp_path,
        "from pathlib import Path\n"
        f"_data = Path({str(secret)!r}).read_text(encoding='utf-8')\n"
        f"Path({str(stolen)!r}).write_text(_data, encoding='utf-8')\n" + _BUILD,
    )

    assert pack is None
    assert "may not import 'pathlib'" in diagnosis
    assert not stolen.exists(), "the read never happened, so nothing could be copied out"


def test_a_pack_that_opens_a_socket_is_refused(tmp_path: Path) -> None:
    pack, diagnosis = _load(
        tmp_path,
        "import socket\n_s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n" + _BUILD,
    )

    assert pack is None
    assert "may not import 'socket'" in diagnosis


def test_a_pack_that_spawns_a_process_is_refused(tmp_path: Path) -> None:
    spawned = tmp_path / "spawned.txt"

    pack, diagnosis = _load(
        tmp_path,
        "import subprocess\n"
        f"subprocess.run(['/bin/sh', '-c', 'echo ran > {spawned}'], check=False)\n" + _BUILD,
    )

    assert pack is None
    assert "may not import 'subprocess'" in diagnosis
    assert not spawned.exists(), "no child process ran"


@pytest.mark.parametrize(
    "module",
    ["os", "sys", "shutil", "importlib", "ctypes", "threading", "multiprocessing", "pickle"],
)
def test_process_level_modules_are_refused_by_name(tmp_path: Path, module: str) -> None:
    """The refusal names the module, so the diagnosis is actionable rather than
    a bare ``ImportError`` an operator has to go and reproduce."""
    pack, diagnosis = _load(tmp_path, f"import {module}\n" + _BUILD)

    assert pack is None
    assert f"may not import {module!r}" in diagnosis


def test_a_submodule_of_a_refused_package_is_refused_too(tmp_path: Path) -> None:
    pack, diagnosis = _load(tmp_path, "import urllib.request\n" + _BUILD)

    assert pack is None
    assert "may not import 'urllib.request'" in diagnosis


def test_a_relative_import_is_refused(tmp_path: Path) -> None:
    """A pack is one ``context.py``; the trust hash covers no sibling module."""
    pack = _make_pack(tmp_path / "ext", "from . import helper\n" + _BUILD)
    (pack / "helper.py").write_text("X = 1\n", encoding="utf-8")
    statuses = discover_packs(
        [pack.parent],
        allow_external=True,
        trust=PackTrust(tmp_path / "trust.json"),
        trust_new=True,
        include_user=False,
    )

    assert statuses["probe_pack"].pack is None
    assert "relative import" in (statuses["probe_pack"].diagnosis or "")


# --- the builtins a pack does not get ----------------------------------------


@pytest.mark.parametrize("name", ["open", "eval", "exec", "compile", "input", "print", "globals"])
def test_the_dangerous_builtins_are_absent(tmp_path: Path, name: str) -> None:
    """Absent, not shadowed: the pack sees a ``NameError``, which the loader
    diagnoses like any other crashing ``context.py``."""
    pack, diagnosis = _load(tmp_path, f"_x = {name}\n" + _BUILD)

    assert pack is None
    assert "NameError" in diagnosis


def test_the_restriction_still_holds_when_build_context_is_called(tmp_path: Path) -> None:
    """Import-time is not the only moment that matters: ``build_context`` runs
    later, per encounter, and resolves its globals through the same mapping."""
    pack, _diagnosis = _load(
        tmp_path,
        "def build_context(encounter, record, cfg):\n    return {'x': open('/etc/hostname')}\n",
    )

    assert pack is not None
    with pytest.raises(NameError):
        pack.build_context(None, None, {})  # type: ignore[attr-defined]


# --- what a legitimate layout still gets -------------------------------------


def test_the_pack_api_a_taught_layout_uses_still_imports(tmp_path: Path) -> None:
    """The imports ``packgen`` writes into every learned ``context.py``, plus
    the helpers the shipped layouts read. If this fails, the Teach is broken."""
    pack, diagnosis = _load(
        tmp_path,
        "from __future__ import annotations\n"
        "import datetime\n"
        "from typing import Any\n"
        "from anastomosis.core.model import Encounter, PatientRecord\n"
        "from anastomosis.core.timeutil import age_display\n"
        "from anastomosis.reconstruct.packctx import record_cache_of\n"
        "from anastomosis.packs.generic_soap.context import build_context as _inner\n"
        "def build_context(encounter: Encounter, record: PatientRecord,"
        " cfg: dict[str, Any]) -> dict[str, Any]:\n"
        "    return _inner(encounter, record, cfg)\n",
    )

    assert pack is not None, diagnosis


def test_a_pack_may_still_define_a_class(tmp_path: Path) -> None:
    """``__build_class__`` is a builtin; a pack that groups its view objects in
    a ``@dataclass`` (as the shipped Practice Fusion layout does) must load."""
    pack, diagnosis = _load(
        tmp_path,
        "from dataclasses import dataclass\n"
        "@dataclass(frozen=True)\n"
        "class _View:\n"
        "    label: str\n"
        "def build_context(encounter, record, cfg):\n"
        "    return {'view': _View(label='ok')}\n",
    )

    assert pack is not None, diagnosis
    assert pack.build_context(None, None, {})["view"].label == "ok"  # type: ignore[attr-defined]


def test_the_built_in_layouts_are_exempt_and_still_load() -> None:
    """Exemption is by ORIGIN. The shipped Practice Fusion layout reads its own
    placeholder logo off disk — first-party code with the application's own
    authority, which is exactly what the restriction is not for."""
    statuses = discover_packs()

    assert statuses["generic_soap"].pack is not None
    assert statuses["practice_fusion_soap"].pack is not None, statuses[
        "practice_fusion_soap"
    ].diagnosis


# --- the tables themselves ----------------------------------------------------


def test_every_allowed_builtin_name_is_a_real_builtin() -> None:
    """A typo in the allowlist would silently withhold a name a layout needs,
    and the symptom would be a ``NameError`` in somebody else's pack."""
    missing = sorted(name for name in PACK_ALLOWED_BUILTINS if not hasattr(builtins, name))

    assert missing == []


@pytest.mark.parametrize(
    "denied", ["os", "sys", "subprocess", "socket", "pathlib", "shutil", "importlib", "ctypes"]
)
def test_the_allowlist_admits_no_process_level_module(denied: str) -> None:
    """A guard on the table, not on a code path: adding one of these to
    ``PACK_ALLOWED_MODULES`` must be a decision someone argues for, not a line
    that slips in beside a legitimate helper."""
    assert denied not in PACK_ALLOWED_MODULES
