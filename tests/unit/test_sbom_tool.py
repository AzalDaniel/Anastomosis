"""The SBOM has to name the version it describes.

``cyclonedx-py --pyproject pyproject.toml`` reads the root component's identity
out of ``[project]``, and this project declares ``dynamic = ["version"]``. Both
shipped SBOMs therefore described a root component with ``version: null`` — and
dropped the installed ``anastomosis`` component from the inventory, because the
root is deduplicated against it. Measured on this tree before the fix:

    root name   : anastomosis
    root version: None
    anastomosis in inventory: ABSENT   (of 84 components)

``cyclonedx-py`` is not a test dependency and this does not run it. What is
checked is the part that was wrong: the version the tool is handed, and the
refusal of a document that does not carry it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tools.sbom import check, package_version, resolved_pyproject

_ROOT = Path(__file__).resolve().parents[2]


def test_the_version_comes_from_the_one_place_it_is_written() -> None:
    """The same file `[tool.hatch.version]` points hatchling at."""
    import anastomosis

    assert package_version() == anastomosis.__version__


def test_the_pyproject_the_tool_reads_carries_a_literal_version() -> None:
    """`dynamic = ["version"]` is what left the root component versionless."""
    resolved = resolved_pyproject("9.9.9")

    assert 'version = "9.9.9"' in resolved
    assert 'dynamic = ["version"]' not in resolved
    # Only that one line changes; nothing is built from this copy, but a
    # mangled `[project]` would still send the tool the wrong dependencies.
    original = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert len(resolved.splitlines()) == len(original.splitlines())


def test_a_renamed_anchor_is_fatal_rather_than_a_silent_no_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `sed` that matches nothing puts the versionless root back, quietly."""
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nname = "anastomosis"\ndynamic = ["readme"]\n', encoding="utf-8")
    monkeypatch.setattr("tools.sbom._ROOT", tmp_path)

    with pytest.raises(SystemExit, match="versionless"):
        resolved_pyproject("9.9.9")


def test_a_document_that_does_not_name_the_version_is_refused(tmp_path: Path) -> None:
    sbom = tmp_path / "sbom.cdx.json"

    sbom.write_text(
        json.dumps({"metadata": {"component": {"name": "anastomosis", "version": None}}}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="expected 'anastomosis'"):
        check(sbom, "9.9.9")

    sbom.write_text(
        json.dumps({"metadata": {"component": {"name": "anastomosis", "version": "9.9.9"}}}),
        encoding="utf-8",
    )
    check(sbom, "9.9.9")  # no raise
