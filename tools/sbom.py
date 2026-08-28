"""Generate a CycloneDX SBOM that can answer the question an SBOM exists for.

``cyclonedx-py --pyproject pyproject.toml`` reads the root component's identity
straight out of ``[project]``. This project declares ``dynamic = ["version"]``,
so there is no version there to read, and the result was:

    root name   : anastomosis
    root version: None
    anastomosis in inventory: ABSENT

— versionless as the root, and dropped from the 84-component inventory because
the root is deduplicated against it. Both shipped SBOMs described a package
whose version they did not state.

So the pyproject the tool reads is a copy with the version resolved into it,
and the emitted document is checked before anyone ships it. Two workflows used
to carry this invocation, once in bash and once in PowerShell; they call this
instead.

    python tools/sbom.py --cyclonedx <exe> --target-python <exe> --out <file>
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
#: The one place the version is written. `pyproject` points hatchling here too,
#: so resolving from it cannot disagree with what the build produces.
_VERSION_FILE = _ROOT / "src" / "anastomosis" / "__init__.py"
_DYNAMIC_VERSION = 'dynamic = ["version"]'


def package_version() -> str:
    """The version hatchling will stamp on the build, read from its source."""
    text = _VERSION_FILE.read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if match is None:
        raise SystemExit(f"no __version__ in {_VERSION_FILE}")
    return match.group(1)


def resolved_pyproject(version: str) -> str:
    """``pyproject.toml`` with the dynamic version replaced by a literal one.

    Only ``[project]`` is touched, and only for the tool to read — nothing is
    ever built from this copy. A missing anchor is fatal rather than a silent
    no-op: a rename here would otherwise put the versionless root back with
    nothing to notice it.
    """
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if text.count(_DYNAMIC_VERSION) != 1:
        raise SystemExit(
            f"pyproject.toml no longer contains exactly one {_DYNAMIC_VERSION!r}; "
            "the SBOM's root component would be versionless again"
        )
    return text.replace(_DYNAMIC_VERSION, f'version = "{version}"')


def check(sbom_path: Path, version: str) -> None:
    """Refuse an SBOM that does not name this package at this version."""
    document = json.loads(sbom_path.read_text(encoding="utf-8"))
    root = document.get("metadata", {}).get("component", {})
    if root.get("name") != "anastomosis" or root.get("version") != version:
        raise SystemExit(
            f"the SBOM's root component is {root.get('name')!r} "
            f"{root.get('version')!r}, expected 'anastomosis' {version!r}"
        )
    print(f"SBOM root: anastomosis {version} ({len(document.get('components', []))} components)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cyclonedx", required=True, help="the cyclonedx-py executable")
    parser.add_argument("--target-python", required=True, help="the interpreter to inventory")
    parser.add_argument("--out", required=True, type=Path, help="where to write the SBOM")
    args = parser.parse_args(argv)

    version = package_version()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        pyproject = Path(tmp) / "pyproject.toml"
        pyproject.write_text(resolved_pyproject(version), encoding="utf-8")
        subprocess.run(  # noqa: S603 - argv list, paths from the caller, no shell
            [
                args.cyclonedx,
                "environment",
                "--pyproject",
                str(pyproject),
                "--output-reproducible",
                "--of",
                "JSON",
                "-o",
                str(args.out),
                args.target_python,
            ],
            check=True,
        )
    check(args.out, version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
