"""Build the Windows GUI + CLI executables with Nuitka (run on a Windows runner).

Produces two self-contained ``--mode=standalone`` (onedir) builds under ``dist/``,
each bundling Chromium (the render path) and all package data:

* ``dist/Anastomosis/Anastomosis.exe`` — the WINDOWED desktop app (no console),
  the Start-menu target;
* ``dist/anast/anast.exe`` — the CONSOLE CLI (``anast``), added to PATH.

Two exes are required because their console modes differ (a windowed GUI must
not flash a console; a CLI must write to the terminal). Each is independently
complete, so the packaging workflow can prove the bundle by running
``dist/anast/anast.exe doctor`` against the FROZEN executable — a missing asset
or an un-bundled Chromium fails that check (and thus the build) rather than
shipping broken.

This script only runs on Windows (the produced exes are Windows PE). The Nuitka
flags are verified against Nuitka 4.x: ``--mode=standalone`` (onedir; NOT onefile,
which would unpack ~280MB of Chromium to %TEMP% each launch), the first-party
``--playwright-include-browser`` (needs ``--enable-plugins=playwright``), the
``pywebview`` plugin, and ``--include-package-data`` for the non-Python assets.
The frozen ``anast doctor`` is the authority on whether the data bundling is
complete; add explicit ``--include-data-dir`` flags below only for assets it
reports missing.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DIST = _ROOT / "dist"


def _version() -> str:
    """The 4-part numeric version Nuitka's ``--product-version`` wants."""
    import anastomosis

    parts = [*anastomosis.__version__.split("+")[0].split("."), "0", "0", "0"][:4]
    return ".".join(p if p.isdigit() else "0" for p in parts)


def _common_flags(version: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "nuitka",
        "--mode=standalone",
        "--assume-yes-for-downloads",  # non-interactive on CI
        "--enable-plugins=playwright,pywebview",
        "--playwright-include-browser=chromium",  # bundle Chromium offline (build-time)
        "--include-package-data=anastomosis",  # the registry/packs/fonts/web/CDA.xsl/etc.
        "--company-name=Anastomosis",
        "--product-name=Anastomosis",
        f"--product-version={version}",
        f"--file-version={version}",
    ]


def _build(main: Path, *, out_subdir: str, exe_name: str, console: str, version: str) -> Path:
    """Run one Nuitka standalone build; return the produced ``.dist`` directory."""
    out_dir = _DIST / "_build" / out_subdir
    if out_dir.exists():
        shutil.rmtree(out_dir)
    cmd = [
        *_common_flags(version),
        f"--windows-console-mode={console}",
        f"--output-filename={exe_name}",
        f"--output-dir={out_dir}",
        str(main),
    ]
    print("nuitka:", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)  # noqa: S603 — cmd is built from trusted constants
    produced = out_dir / f"{main.stem}.dist"
    if not produced.is_dir():
        raise SystemExit(f"nuitka did not produce the expected dist dir: {produced}")
    return produced


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("build_windows.py builds Windows executables; run it on a Windows runner.")

    version = _version()
    print(f"building Anastomosis {version} (Nuitka standalone)", flush=True)

    gui_dist = _build(
        _ROOT / "src" / "anastomosis" / "gui" / "__main__.py",
        out_subdir="gui",
        exe_name="Anastomosis.exe",
        console="disable",  # windowed: no console window for the desktop app
        version=version,
    )
    cli_dist = _build(
        _ROOT / "src" / "anastomosis" / "cli.py",
        out_subdir="cli",
        exe_name="anast.exe",
        console="force",  # console: the CLI writes to the terminal it ran from
        version=version,
    )

    # Lay the two builds out under dist/ with clean directory names for the
    # installer's [Files] sections.
    for src, name in ((gui_dist, "Anastomosis"), (cli_dist, "anast")):
        dest = _DIST / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(src), str(dest))
        print(f"staged {dest}", flush=True)

    gui_exe = _DIST / "Anastomosis" / "Anastomosis.exe"
    cli_exe = _DIST / "anast" / "anast.exe"
    for exe in (gui_exe, cli_exe):
        if not exe.is_file():
            raise SystemExit(f"expected executable missing: {exe}")
    print(f"OK: {gui_exe}\nOK: {cli_exe}", flush=True)


if __name__ == "__main__":
    main()
