# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""Pins the Playwright build version to ONE source of truth.

Invariant: ``packaging/constraints.txt`` carries the single ``playwright==``
pin, and every build surface resolves Playwright through it. The rendering
goldens (tests/e2e/goldens/*.json) and the Chromium bundled into the Windows
installer are only reproducible if the CI test lane, golden regeneration, and
the Windows package build all install the SAME Playwright — one pin to rule
the builds. The library floor in pyproject.toml stays open for users, but it
must never float ABOVE the build pin, or a build would resolve something the
floor already forbids.

This test keeps that live by parsing the constraints file and asserting the
two workflows point at it (never a literal pin) and the pyproject floor sits
at or below the pin.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSTRAINTS = REPO_ROOT / "packaging" / "constraints.txt"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
WINDOWS_YML = REPO_ROOT / ".github" / "workflows" / "windows-package.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse a dotted version into an int tuple for ordering.

    Deliberately avoids importing ``packaging`` — the pins here are plain
    dotted numerics (``1.56.0``, ``1.46``), so split-on-dots-compare-ints is
    enough, and Python's tuple ordering handles differing lengths correctly.
    """
    return tuple(int(part) for part in version.split("."))


def _pinned_playwright() -> str:
    """The single ``playwright==X`` version from packaging/constraints.txt."""
    pins: list[str] = []
    for raw in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"playwright==([0-9][0-9.]*)", line)
        if match:
            pins.append(match.group(1))
    assert len(pins) == 1, (
        f"packaging/constraints.txt must carry exactly one playwright== pin; found {pins!r}. "
        "This file is the single source of truth for the Playwright build version."
    )
    return pins[0]


def _pyproject_floors() -> list[str]:
    """Every ``playwright>=X`` floor declared in pyproject.toml."""
    return re.findall(r"playwright>=([0-9][0-9.]*)", PYPROJECT.read_text(encoding="utf-8"))


def test_constraints_file_has_one_pin() -> None:
    # _pinned_playwright asserts exactly-one internally; call it for that guard.
    assert _pinned_playwright()


def test_workflows_resolve_through_constraints() -> None:
    """(a) both build surfaces install via -c packaging/constraints.txt, and
    (b) neither workflow carries a literal playwright== pin anymore."""
    for path in (CI_YML, WINDOWS_YML):
        text = path.read_text(encoding="utf-8")
        install_via_constraints = any(
            "pip install" in line and "-c packaging/constraints.txt" in line
            for line in text.splitlines()
        )
        assert install_via_constraints, (
            f"{path.name} must install Playwright via `-c packaging/constraints.txt` "
            "so the build pin is the single source of truth."
        )
        assert "playwright==" not in text, (
            f"{path.name} still contains a literal `playwright==` pin. Remove it and let "
            "packaging/constraints.txt govern the version via `-c`."
        )


def test_pyproject_floor_not_above_pin() -> None:
    """The open library floor must never sit above the build pin, or a build
    would resolve a version the floor forbids."""
    pin = _version_tuple(_pinned_playwright())
    floors = _pyproject_floors()
    assert floors, "expected at least one `playwright>=` floor in pyproject.toml"
    for floor in floors:
        assert _version_tuple(floor) <= pin, (
            f"pyproject floor playwright>={floor} is above the build pin "
            f"{_pinned_playwright()} in packaging/constraints.txt — bump the pin or "
            "lower the floor so builds stay resolvable."
        )


def test_windows_cache_key_tracks_constraints() -> None:
    """(d) the Windows browser cache key hashes the constraints file, so a pin
    bump automatically invalidates the cached Chromium."""
    key_lines = [
        line
        for line in WINDOWS_YML.read_text(encoding="utf-8").splitlines()
        if "ms-playwright" in line and "key:" in line
    ]
    assert len(key_lines) == 1, (
        f"expected exactly one ms-playwright cache key line in {WINDOWS_YML.name}; "
        f"found {len(key_lines)}"
    )
    assert "hashFiles('packaging/constraints.txt')" in key_lines[0], (
        "the ms-playwright cache key must be derived from "
        "hashFiles('packaging/constraints.txt') so a pin bump invalidates the browser cache."
    )
