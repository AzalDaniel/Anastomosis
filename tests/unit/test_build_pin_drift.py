"""Pins EVERY build version to one source of truth, not just Playwright.

``packaging/constraints.txt`` carries the build pins, and every build
surface resolves through it: the rendering goldens and the Chromium
bundled into the Windows installer are only reproducible if CI, golden
regeneration and the Windows build all install the SAME Playwright. The
library floor in pyproject.toml stays open for users, but must never
float ABOVE the build pin (#142) — nor may anything the file pins next.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSTRAINTS = REPO_ROOT / "packaging" / "constraints.txt"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
WINDOWS_YML = REPO_ROOT / ".github" / "workflows" / "windows-package.yml"
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse a dotted version into an int tuple for ordering. Deliberately
    avoids importing ``packaging``: the pins here are plain dotted numerics
    (``1.56.0``, ``1.46``), so split-on-dots-compare-ints is enough."""
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


def _constrained_packages() -> set[str]:
    """Every package name ``packaging/constraints.txt`` pins."""
    names = {
        match.group(1).lower()
        for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines()
        if (match := re.match(r"^([A-Za-z0-9._-]+)==", line.strip()))
    }
    assert names, "packaging/constraints.txt pins nothing at all"
    return names


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


#: The versions that decide what ships, and therefore have to be pinned
#: somewhere. Named here rather than derived, because the derivable form —
#: "everything a workflow installs alongside `-c`" — would also demand pins for
#: pytest and hypothesis, which are deliberately floating: they shape the test
#: run, not the artifact.
_MUST_BE_PINNED = {
    "playwright": "the rendering goldens and the Chromium inside the installer",
    "pywebview": "whether the frozen Windows GUI starts at all",
    "nuitka": "the frozen layout the installer carries",
    "cyclonedx-bom": "the shape and spec level of a shipped SBOM",
}


def test_the_pins_that_decide_what_ships_are_all_governed() -> None:
    """Moving a pin out of the file is how it starts floating unnoticed:
    `-c packaging/constraints.txt` constrains, it does not pin, so a
    deleted line here resolves whatever is newest while the `-c` still
    looks like a guarantee."""
    pinned = _constrained_packages()
    for package, why in _MUST_BE_PINNED.items():
        assert package in pinned, (
            f"packaging/constraints.txt no longer pins {package}, which governs "
            f"{why}. The build surfaces install it through this file, so without "
            "a line here it floats."
        )


def test_no_workflow_pins_a_constrained_package() -> None:
    """A second copy of a pin is a pin that can disagree with the first
    (#142): a package pinned literally in two workflows can have one
    bumped and the other forgotten, shipping artifacts built by different
    tool versions with nothing to notice."""
    for path in (CI_YML, WINDOWS_YML, RELEASE_YML):
        text = path.read_text(encoding="utf-8")
        for package in _constrained_packages():
            assert f"{package}==" not in text.lower(), (
                f"{path.name} pins {package} directly. packaging/constraints.txt "
                "governs it — install with `-c packaging/constraints.txt` instead, "
                "so the version lives in one place."
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
