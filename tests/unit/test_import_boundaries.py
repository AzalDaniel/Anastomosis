"""Package import boundaries, checked two ways: a clean subprocess reading
``sys.modules`` (so the runner's own imports cannot bias it) for what loads
eagerly, and the syntax tree for rule 76's lazy in-function edges.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path


def _modules_after_import(target: str) -> set[str]:
    """Import ``target`` in a clean subprocess and return ``sys.modules``."""
    script = textwrap.dedent(f"""
        import sys
        import {target}  # noqa: F401
        # Emit one module name per line on stdout; the parent splits and sets it.
        for name in sorted(sys.modules):
            print(name)
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return set(proc.stdout.split())


def test_gui_does_not_import_cli() -> None:
    """Importing the GUI never loads the CLI (rule 107): the upload-attach
    seam they share is :mod:`anastomosis.deliver.browser.attach`."""
    loaded = _modules_after_import("anastomosis.gui")
    forbidden = {"anastomosis.cli"}
    leaked = forbidden & loaded
    assert not leaked, (
        f"anastomosis.gui leaked CLI dependency into sys.modules: {sorted(leaked)}. "
        "The GUI must use anastomosis.deliver.browser.attach.attach_destination "
        "instead of any CLI-private helper."
    )


def test_gui_does_not_import_cli_commands() -> None:
    """The same boundary one layer down: every command group imports
    ``anastomosis.cli`` at its top, so a GUI edge to one drags in the CLI."""
    loaded = _modules_after_import("anastomosis.gui")
    leaked = {name for name in loaded if name.startswith("anastomosis.cli_commands")}
    assert not leaked, (
        f"anastomosis.gui leaked cli_commands modules into sys.modules: {sorted(leaked)}. "
        "The GUI must not import any CLI command group (they import anastomosis.cli)."
    )


def test_cli_does_not_eagerly_import_source_adapters_or_destinations() -> None:
    """No source adapter or destination client loads just by importing the
    CLI (rule 75); each is lazy per command. ``deliver.fhir_api``'s own
    near-empty init is exempt, its heavy children are not."""
    loaded = _modules_after_import("anastomosis.cli")
    forbidden = {
        "anastomosis.sources.ccda",
        "anastomosis.sources.fhir_r4",
        "anastomosis.sources.oracle_ehi",
        "anastomosis.sources.pf_tebra",
        "anastomosis.deliver.browser",
        "anastomosis.deliver.fhir_api.client",
        "anastomosis.deliver.fhir_api.destination",
    }
    leaked = forbidden & loaded
    assert not leaked, f"anastomosis.cli eagerly imported: {sorted(leaked)}"


def test_cli_does_not_eagerly_import_the_greeting_mark() -> None:
    """The vessel mark is drawn only for a person at a terminal (rule 75):
    no named command pays for the grid, the ramp or Rich's live display."""
    loaded = _modules_after_import("anastomosis.cli")
    forbidden = {
        "anastomosis.core.vesselmark",
        "anastomosis.core.vesselmark_data",
        "rich.live",
    }
    leaked = forbidden & loaded
    assert not leaked, f"anastomosis.cli eagerly imported: {sorted(leaked)}"


def test_browser_attach_module_loads_without_playwright_extra() -> None:
    """Playwright is imported inside :func:`attach_destination`, so an install
    without the ``deliver-browser`` extra still loads the CLI and GUI."""
    loaded = _modules_after_import("anastomosis.deliver.browser.attach")
    assert "playwright" not in loaded
    assert "playwright.sync_api" not in loaded


def test_cli_make_destination_delegates_to_attach_destination() -> None:
    """A thin lazy-import wrapper that delegates straight to
    :func:`attach_destination`. A module-level assignment instead would make
    importing the CLI load the whole upload engine."""
    from unittest.mock import patch

    from anastomosis.cli import _make_destination

    sentinel = object()
    with patch(
        "anastomosis.deliver.browser.attach.attach_destination", return_value=sentinel
    ) as mock_attach:
        result = _make_destination("http://127.0.0.1:9222", "loaded")

    mock_attach.assert_called_once_with("http://127.0.0.1:9222", "loaded")
    assert result is sentinel


# --- public verification imports (circular-import regression) --------------
#
# The suite's own import order can mask a cycle between verify.composite and
# browser.reports; each test below imports in a fresh interpreter instead.


def test_layered_verifier_public_import_in_fresh_process() -> None:
    """The public import succeeds in a clean interpreter: no cycle between
    :mod:`.verify.composite` and :mod:`.browser.reports`."""
    loaded = _modules_after_import("anastomosis.deliver.verify")
    assert "anastomosis.deliver.verify.composite" in loaded
    # Best-effort sanity: the leaf types module is loaded too.
    assert "anastomosis.deliver.verify.types" in loaded


def test_level_coverage_imports_from_both_sites() -> None:
    """:class:`LevelCoverage` is published in two places for back-compat:
    its canonical home (:mod:`.verify.types`) and a re-export from
    :mod:`.verify.composite`. Both imports must succeed in a fresh process
    and resolve to the SAME class object — the alias-identity contract
    downstream consumers rely on."""
    script = textwrap.dedent("""
        from anastomosis.deliver.verify.composite import LevelCoverage as A
        from anastomosis.deliver.verify.types import LevelCoverage as B
        assert A is B, f"LevelCoverage drift: composite={A!r} types={B!r}"
        print("ok")
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert proc.stdout.strip() == "ok"


def test_browser_reports_does_not_directly_import_verify_composite() -> None:
    """``browser.reports`` must NOT *directly* import from
    :mod:`.verify.composite`: that import cycles back through
    ``browser.errors`` and ``browser/__init__.py`` into ``.reports``
    itself, resolving ``LevelCoverage`` against a partially-initialized
    module. Its canonical home is the leaf module :mod:`.verify.types`."""
    from pathlib import Path

    reports_src = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "anastomosis"
        / "deliver"
        / "browser"
        / "reports.py"
    ).read_text(encoding="utf-8")
    assert "from anastomosis.deliver.verify.composite" not in reports_src, (
        "browser/reports.py reintroduced a direct import from verify.composite. "
        "That's the cycle this test guards against. Import LevelCoverage from "
        "anastomosis.deliver.verify.types instead (the leaf module)."
    )
    # The leaf import is what the fix expects to see.
    assert "from anastomosis.deliver.verify.types import LevelCoverage" in reports_src, (
        "browser/reports.py must import LevelCoverage from the leaf .verify.types "
        "module (the break-the-cycle fix)."
    )


# --- the core boundary (rule 76) -------------------------------------------
#
# Everything above core/ may import it; it imports nothing above itself. The
# edges this guards are lazy ones inside functions, so it reads the syntax
# tree rather than sys.modules.

SRC = Path(__file__).resolve().parents[2] / "src"
CORE = SRC / "anastomosis" / "core"


def _package_of(path: Path) -> str:
    """The dotted package a relative import inside `path` resolves against."""
    parts = path.relative_to(SRC).with_suffix("").parts
    return ".".join(parts if parts[-1] == "__init__" else parts[:-1])


def _outward_imports(path: Path) -> list[tuple[int, str]]:
    """Every `anastomosis` module imported from outside `anastomosis.core`,
    with its line. Relative imports resolve first (`core/model` and
    `core/fhir` are written with them); the walk reaches function bodies and
    `TYPE_CHECKING` blocks."""
    package = _package_of(path)
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # One dot is the file's own package; each further dot climbs
                # a level, and rsplit clamps at the top package.
                base = package.rsplit(".", node.level - 1)[0]
                targets = [f"{base}.{node.module}" if node.module else base]
            elif node.module:
                targets = [node.module]
        else:
            continue
        found.extend(
            (node.lineno, target)
            for target in targets
            if target.startswith("anastomosis.")
            and target != "anastomosis.core"
            and not target.startswith("anastomosis.core.")
        )
    return found


def test_core_imports_nothing_outward() -> None:
    """Rule 76: nothing under `core/` imports another `anastomosis` package —
    not `deliver`, `pipeline`, `reconstruct`, `sources`, `qa`,
    `destinations`, `packgen`, `gui`, nor `commands`."""
    scanned = sorted(CORE.rglob("*.py"))
    assert CORE / "identity.py" in scanned, f"walked nothing under {CORE}"
    offenders = [
        f"{path.relative_to(SRC)}:{line} imports {target}"
        for path in scanned
        for line, target in _outward_imports(path)
    ]
    assert not offenders, (
        "core/ imports outside anastomosis.core (rule 76): "
        + "; ".join(offenders)
        + ". The command layer lives in commands/, not in the primitives package."
    )


# --- the import graph: no package re-exports (rule 75) ---------------------
#
# `deliver/browser/__init__.py` and `destinations/__init__.py` are docstring
# markers, so one submodule costs one submodule; `.persist` proves it, since
# `anast upload` reads a manifest long after the render run. One edge stays
# open: `.persist` needs `VerifyPolicy` at runtime, importing
# `deliver.verify.types` runs `deliver/verify/__init__.py`, and that init has
# six callers of `LayeredVerifier` — so `verify.composite` is absent below.

#: Module `.persist` must not load -> the import that would re-introduce it.
_PERSIST_MUST_NOT_LOAD = {
    "sqlite3": "anastomosis.deliver.browser.tracking, the ledger",
    "anastomosis.deliver.browser.engine": "a re-export in deliver/browser/__init__.py",
    "anastomosis.deliver.browser.tracking": "a re-export in deliver/browser/__init__.py",
    "anastomosis.deliver.browser.cdp": "a re-export in deliver/browser/__init__.py",
    "anastomosis.destinations.browserpack": "a re-export in destinations/__init__.py",
}


def test_manifest_writer_does_not_load_the_upload_engine() -> None:
    """Importing the manifest writer loads the manifest writer: not the SQLite
    ledger, the upload engine, the CDP client, nor the destination pack
    adapter. One re-export in either package ``__init__`` puts every one of
    them back on the import path."""
    loaded = _modules_after_import("anastomosis.deliver.browser.persist")
    leaked = sorted(set(_PERSIST_MUST_NOT_LOAD) & loaded)
    assert not leaked, (
        "importing the manifest writer loaded "
        + "; ".join(f"{name}, re-introduced by {_PERSIST_MUST_NOT_LOAD[name]}" for name in leaked)
        + ". Import each name from the module that defines it (rule 75)."
    )


def test_verification_ladder_does_not_load_the_sqlite_ledger() -> None:
    """The ladder verifies delivered bytes; the ledger records upload
    progress. Importing :mod:`anastomosis.deliver.verify` opens only the
    first: it reaches ``browser.errors`` for two exception types, and a bare
    ``browser/__init__.py`` keeps that from meaning the whole package."""
    loaded = _modules_after_import("anastomosis.deliver.verify")
    assert "sqlite3" not in loaded, (
        "importing the verification ladder loaded sqlite3, re-introduced by "
        "anastomosis.deliver.browser.tracking — the ladder imports "
        "browser.errors, and a re-export in deliver/browser/__init__.py makes "
        "that the ledger too (rule 75)."
    )
