"""Pack the ALREADY-BUILT Windows layout a second way: an MSIX for the Store.

The Inno Setup installer and this package are two doors onto one build. This
script never compiles anything: ``build_windows.py`` has already produced the
two Nuitka standalone dists (``dist/Anastomosis/Anastomosis.exe`` and
``dist/anast/anast.exe``, each bundling Chromium and every data asset), and
this stages those exact directories under one package root, renders
``AppxManifest.xml.in`` into it, drops in the three committed logo PNGs, and
runs the Windows SDK's ``makeappx.exe pack``. Building a second time would
double a forty-minute job and let the two artifacts drift apart.

Why an MSIX at all: the installer is unsigned, so every download meets
SmartScreen. The Microsoft Store re-signs each package it ingests with
Microsoft's own certificate, so a Store install carries a trusted publisher
signature this project does not have to buy. Which is also why NOTHING HERE
SIGNS: the Store's signature is the point, and a self-signed package would only
teach people to install certificates they should not. The produced ``.msix`` is
therefore not double-clickable — it is a submission artifact.

The app needed no changes to live in the container. It writes only to
``~/.anastomosis``, its own WebView2 profile under ``%LOCALAPPDATA``, and the
output directories the operator chooses; everything under the install directory
is read-only bundled asset.

Run on a Windows runner, after ``build_windows.py``::

    python packaging/build_msix.py --version 0.7.0

Identity is a submission-time decision, so ``--identity-name`` and
``--identity-publisher`` default to local-validation values and the real ones
come from Partner Center (see the header of ``AppxManifest.xml.in``).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGING = Path(__file__).resolve().parent
_DIST = _ROOT / "dist"

#: The manifest template and the three committed logo renditions
#: (tools/make_icons.py regenerates the PNGs from the one SVG master).
_MANIFEST_TEMPLATE = _PACKAGING / "AppxManifest.xml.in"
_LOGO_DIR = _PACKAGING / "msix-assets"
_LOGOS = ("Square150x150Logo.png", "Square44x44Logo.png", "StoreLogo.png")

#: Where the staged package root is assembled, and where the package lands.
#: Beside the installer on purpose: one directory the workflow uploads, one
#: directory the release job attaches from.
_STAGING = _DIST / "_msix"
_OUTPUT_DIR = _DIST / "installer"

#: What build_windows.py produced -> where it sits inside the package. The
#: names on the right mirror the installed layout anastomosis.iss lays down
#: ({app}\gui, {app}\cli), so both artifacts describe themselves the same way,
#: and AppxManifest.xml.in points its Executable attributes at them.
_LAYOUT = {"Anastomosis": "gui", "anast": "cli"}
#: The executable each staged directory must actually contain. This is the
#: whole "is this the built layout?" question: an empty or half-built dist is
#: refused here rather than packed into a Store submission that installs to
#: nothing.
_REQUIRED_EXES = {"gui": "Anastomosis.exe", "cli": "anast.exe"}

#: The third-party texts the app redistributes, laid down beside it exactly as
#: the installer lays them down ({app}\licenses). The obligation follows the
#: bytes, not the container they ship in.
_LICENSE_INVENTORY = _ROOT / "THIRD_PARTY_LICENSES.md"
_LICENSE_TEXTS = _ROOT / "assets" / "licenses"

#: Where the Windows SDK puts its tools. The versioned layout
#: (bin\10.0.22621.0\x64\makeappx.exe) is what every SDK since Windows 10 uses;
#: both Program Files roots are searched because a runner image is free to
#: install the SDK under either.
_SDK_ROOTS = (
    Path(r"C:\Program Files (x86)\Windows Kits\10"),
    Path(r"C:\Program Files\Windows Kits\10"),
)
_MAKEAPPX_GLOB = "bin/*/x64/makeappx.exe"

#: Every placeholder the template is allowed to carry. Anything matching this
#: shape that survives substitution is a bug in the template or here.
_PLACEHOLDER_RE = re.compile(r"@[A-Z0-9_]+@")

#: Local-validation identity. Partner Center assigns the real pair at
#: submission; these let a developer pack and inspect a package today.
_DEFAULT_IDENTITY_NAME = "AzalDaniel.Anastomosis"
_DEFAULT_IDENTITY_PUBLISHER = "CN=Azal Daniel"


def quad_version(version: str) -> str:
    """The 4-part numeric version an MSIX ``Identity`` requires.

    Deliberately the same normalisation ``build_windows.py``'s ``_version()``
    applies to the Nuitka product version: the package and the exes inside it
    must not disagree about which build this is. A local segment (``0.7.0+x``)
    is dropped and a non-numeric part (``0.8.0rc1``) becomes 0, exactly as it
    does there.

    The fourth part is the revision, which the Store requires to be 0 for a
    submitted package — and always is here, since this project versions in
    three parts.
    """
    parts = [*version.split("+")[0].split("."), "0", "0", "0"][:4]
    return ".".join(p if p.isdigit() else "0" for p in parts)


def render_manifest(
    template: str,
    *,
    version: str,
    identity_name: str,
    identity_publisher: str,
) -> str:
    """Substitute every placeholder in the manifest template, or refuse.

    The refusal is the point. ``makeappx`` will happily pack a manifest whose
    Identity reads ``@IDENTITY_NAME@`` — it is a schema-valid string — and
    Partner Center rejects it hours later at submission. Failing here names the
    placeholder while someone is still looking at the build log.
    """
    rendered = template
    for token, value in (
        ("@VERSION@", version),
        ("@IDENTITY_NAME@", identity_name),
        ("@IDENTITY_PUBLISHER@", identity_publisher),
    ):
        rendered = rendered.replace(token, value)
    left_over = sorted(set(_PLACEHOLDER_RE.findall(rendered)))
    if left_over:
        raise SystemExit(
            f"{_MANIFEST_TEMPLATE.name} carries placeholders this script does not "
            f"substitute: {left_over}. Give each one a value here — an unsubstituted "
            "placeholder packs cleanly and is refused at submission instead."
        )
    return rendered


def sdk_version(makeappx: Path) -> tuple[int, ...]:
    """The SDK version a ``makeappx.exe`` path sits under, as an int tuple.

    ``bin\\10.0.22621.0\\x64\\makeappx.exe`` -> ``(10, 0, 22621, 0)``. A
    directory that is not a dotted version sorts below every real one rather
    than raising: the glob decides what counts as a candidate, this only
    decides which candidate is newest.
    """
    parts = makeappx.parent.parent.name.split(".")
    return tuple(int(part) if part.isdigit() else 0 for part in parts)


def find_makeappx(roots: Iterable[Path]) -> Path:
    """The newest installed SDK's ``makeappx.exe``, or a loud refusal.

    Newest wins because an older SDK's packer can reject manifest elements a
    newer schema defines, and the failure it gives is a schema error that reads
    like a manifest bug rather than a tooling one.
    """
    candidates = sorted({found for root in roots for found in root.glob(_MAKEAPPX_GLOB)})
    if not candidates:
        searched = ", ".join(f"{root}\\{_MAKEAPPX_GLOB}" for root in roots)
        raise SystemExit(
            f"makeappx.exe was not found ({searched}). It ships with the Windows "
            "10/11 SDK; a GitHub windows-latest runner has it pre-installed, so on "
            "CI this means the image changed rather than that a step was skipped."
        )
    return max(candidates, key=sdk_version)


def stage_package_root(dist: Path, staging: Path, manifest: str) -> Path:
    """Assemble exactly what ``makeappx`` will pack, and nothing else.

    A staging root exists because ``makeappx pack`` takes ONE directory and
    packs it verbatim, while the build leaves the two dists as siblings under
    ``dist/`` with names the package must not use. Staging is rebuilt from
    scratch every run, so a rename or a removal upstream can never leave a
    stale file inside a submitted package.
    """
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for built, inside in sorted(_LAYOUT.items()):
        _stage_built_dist(dist / built, staging / inside, _REQUIRED_EXES[inside])
    _stage_logos(staging / "Assets")
    _stage_licenses(staging / "licenses")
    (staging / "AppxManifest.xml").write_text(manifest, encoding="utf-8")
    return staging


def _stage_built_dist(source: Path, dest: Path, exe_name: str) -> None:
    """Copy one Nuitka dist into the package root, having proved it is one."""
    exe = source / exe_name
    if not exe.is_file():
        raise SystemExit(
            f"this is not the built Windows layout: {exe} is missing. Run "
            "packaging/build_windows.py first — the MSIX packs the SAME build the "
            "installer packages, never a second one."
        )
    shutil.copytree(source, dest)


def _stage_logos(assets: Path) -> None:
    """The three committed logo renditions the manifest names."""
    assets.mkdir()
    for logo in _LOGOS:
        source = _LOGO_DIR / logo
        if not source.is_file():
            raise SystemExit(
                f"missing MSIX logo asset: {source}. Regenerate the branding "
                "renditions with `python tools/make_icons.py`."
            )
        shutil.copy2(source, assets / logo)


def _stage_licenses(dest: Path) -> None:
    """The third-party texts, exactly as the installer ships them."""
    dest.mkdir()
    shutil.copy2(_LICENSE_INVENTORY, dest / _LICENSE_INVENTORY.name)
    for text in sorted(_LICENSE_TEXTS.glob("*.txt")):
        shutil.copy2(text, dest / text.name)


def makeappx_argv(makeappx: Path, package_root: Path, output: Path) -> list[str]:
    """The exact ``makeappx pack`` invocation.

    ``/o`` overwrites a previous run's file, so re-running the step is
    idempotent instead of failing on the artifact it produced last time. There
    is no signing flag and no signtool call anywhere in this script — see the
    module docstring for why that is deliberate rather than unfinished.
    """
    return [str(makeappx), "pack", "/o", "/d", str(package_root), "/p", str(output)]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--version",
        required=True,
        help="The package version, as the workflow resolved it (e.g. 0.7.0).",
    )
    parser.add_argument(
        "--dist",
        type=Path,
        default=_DIST,
        help="The directory build_windows.py wrote its two dists into.",
    )
    parser.add_argument(
        "--identity-name",
        default=_DEFAULT_IDENTITY_NAME,
        help="Partner Center's Package/Identity/Name (default: a local-validation value).",
    )
    parser.add_argument(
        "--identity-publisher",
        default=_DEFAULT_IDENTITY_PUBLISHER,
        help="Partner Center's Package/Identity/Publisher (default: a local-validation value).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if sys.platform != "win32":
        raise SystemExit("build_msix.py packs a Windows MSIX; run it on a Windows runner.")

    manifest = render_manifest(
        _MANIFEST_TEMPLATE.read_text(encoding="utf-8"),
        version=quad_version(args.version),
        identity_name=args.identity_name,
        identity_publisher=args.identity_publisher,
    )
    # The FILE keeps the package version the installer and the SBOM use
    # (Anastomosis-0.7.0.msix beside Anastomosis-Setup-0.7.0.exe); only the
    # manifest carries the 4-part quad the format requires.
    output = _OUTPUT_DIR / f"Anastomosis-{args.version}.msix"
    print(f"packing Anastomosis {args.version} (MSIX, unsigned — the Store signs it)", flush=True)

    staging = stage_package_root(args.dist, _STAGING, manifest)
    print(f"staged {staging}", flush=True)
    makeappx = find_makeappx(_SDK_ROOTS)
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = makeappx_argv(makeappx, staging, output)
    print("makeappx:", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)  # noqa: S603 — argv list, absolute exe path, no shell

    if not output.is_file():
        raise SystemExit(f"makeappx reported success but produced no package: {output}")
    print(f"OK: {output} ({output.stat().st_size} bytes)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
