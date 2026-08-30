"""The Microsoft Store packaging lane, tested off Windows.

``makeappx.exe`` ships with the Windows SDK and the package it produces can
only be built on a Windows runner, so the one integration proof — a real
``makeappx pack`` over the real Nuitka layout — lives in the packaging
workflow and nowhere else. It cannot be moved here.

Everything the packing DECIDES can be, and is: which version quad the manifest
carries, what the manifest says once its placeholders are filled, which
``makeappx.exe`` is chosen out of several installed SDKs, what the staged
package root contains, and the exact argv. Plus the three refusals the lane
turns on — no SDK, a dist that is not the built layout, a placeholder nobody
substituted — because each of those is a build that must stop rather than a
package that reaches Partner Center wrong.

The last section pins the workflow wiring: the MSIX is packed from the build
that already happened, and it rides beside the installer into both the CI
artifact and the GitHub release.
"""

from __future__ import annotations

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGING = REPO_ROOT / "packaging"
WINDOWS_YML = REPO_ROOT / ".github" / "workflows" / "windows-package.yml"

#: The MSIX manifest namespaces, as AppxManifest.xml.in declares them.
NS = {
    "": "http://schemas.microsoft.com/appx/manifest/foundation/windows10",
    "uap": "http://schemas.microsoft.com/appx/manifest/uap/windows10",
    "uap3": "http://schemas.microsoft.com/appx/manifest/uap/windows10/3",
    "desktop": "http://schemas.microsoft.com/appx/manifest/desktop/windows10",
    "rescap": (
        "http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
    ),
}


def _load(name: str) -> ModuleType:
    """Load a packaging script by path.

    They are build scripts, not modules of the installed package, and importing
    one must not require Windows — the same route ``test_smoke_windows_liveness``
    takes to the installer smoke test.
    """
    spec = importlib.util.spec_from_file_location(name, PACKAGING / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def msix() -> ModuleType:
    return _load("build_msix")


@pytest.fixture
def rendered(msix: ModuleType) -> ET.Element:
    """The SHIPPED template, rendered the way the build renders it."""
    xml = msix.render_manifest(
        (PACKAGING / "AppxManifest.xml.in").read_text(encoding="utf-8"),
        version="0.7.0.0",
        identity_name="12345AzalDaniel.Anastomosis",
        identity_publisher="CN=feedface-0000-0000-0000-000000000000",
    )
    return ET.fromstring(xml)


def _built_dist(root: Path) -> Path:
    """A stand-in for what build_windows.py leaves under ``dist/``.

    One file per exe plus a companion, which is all the staging cares about —
    the real dists are ~2600 files each and copying a tree is the same call
    whether the tree is two files or two thousand.
    """
    dist = root / "dist"
    for name, exe in (("Anastomosis", "Anastomosis.exe"), ("anast", "anast.exe")):
        (dist / name).mkdir(parents=True)
        (dist / name / exe).write_text("MZ", encoding="utf-8")
        (dist / name / "python312.dll").write_text("dll", encoding="utf-8")
    return dist


# --- the version quad -------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("0.7.0", "0.7.0.0"),
        ("1", "1.0.0.0"),
        ("1.2.3.4", "1.2.3.4"),
        # A local version segment is build metadata, and the MSIX Version
        # attribute has nowhere to put it.
        ("0.7.0+dirty", "0.7.0.0"),
        # A non-numeric part becomes 0 — the format admits digits only.
        ("0.8.0rc1", "0.8.0.0"),
    ],
)
def test_quad_version_normalises_to_four_numeric_parts(
    msix: ModuleType, given: str, expected: str
) -> None:
    assert msix.quad_version(given) == expected


def test_the_package_and_the_exes_inside_it_carry_the_same_quad(msix: ModuleType) -> None:
    """One build, one version.

    Nuitka stamps ``build_windows.py``'s quad into both exes; the manifest
    around them carries this one. If the two normalisations ever diverge, a
    package would claim a version its own binaries deny — so they are asserted
    equal against the live package version rather than trusted to stay in step.
    """
    import anastomosis

    build_windows = _load("build_windows")
    assert msix.quad_version(anastomosis.__version__) == build_windows._version()


# --- rendering the manifest -------------------------------------------------


def test_render_manifest_substitutes_identity_and_version(msix: ModuleType) -> None:
    out = msix.render_manifest(
        '<Identity Name="@IDENTITY_NAME@" Publisher="@IDENTITY_PUBLISHER@" Version="@VERSION@" />',
        version="0.7.0.0",
        identity_name="12345AzalDaniel.Anastomosis",
        identity_publisher="CN=feedface-0000-0000-0000-000000000000",
    )
    assert 'Name="12345AzalDaniel.Anastomosis"' in out
    assert 'Publisher="CN=feedface-0000-0000-0000-000000000000"' in out
    assert 'Version="0.7.0.0"' in out


def test_render_manifest_refuses_a_placeholder_it_does_not_know(msix: ModuleType) -> None:
    """A placeholder nobody substituted packs cleanly and is refused at
    submission hours later. It fails here instead, naming itself."""
    with pytest.raises(SystemExit) as excinfo:
        msix.render_manifest(
            '<Identity Name="@IDENTITY_NAME@" Publisher="@IDENTITY_PUBLISHER@" '
            'Version="@VERSION@" Tag="@RELEASE_CHANNEL@" />',
            version="0.7.0.0",
            identity_name="n",
            identity_publisher="CN=p",
        )
    assert "@RELEASE_CHANNEL@" in str(excinfo.value)


def test_the_shipped_template_renders_with_nothing_left_over(rendered: ET.Element) -> None:
    """The positive half: the real template's placeholders are exactly the
    three the script substitutes, so a build of it never trips the refusal."""
    assert rendered.tag == f"{{{NS['']}}}Package"


def test_the_manifest_identifies_an_x64_full_trust_desktop_app(rendered: ET.Element) -> None:
    identity = rendered.find("Identity", NS)
    assert identity is not None
    assert identity.get("ProcessorArchitecture") == "x64"
    assert identity.get("Version") == "0.7.0.0"

    application = rendered.find("Applications/Application", NS)
    assert application is not None
    assert application.get("EntryPoint") == "Windows.FullTrustApplication"
    # The GUI exe, at the path staging puts it — the installed layout's own
    # spelling ({app}\gui\Anastomosis.exe).
    assert application.get("Executable") == r"gui\Anastomosis.exe"

    capabilities = [c.get("Name") for c in rendered.findall("Capabilities/rescap:Capability", NS)]
    assert capabilities == ["runFullTrust"], (
        "a packaged Win32 app declares runFullTrust and, for this app, nothing "
        f"else — it asks the OS for no other capability. Found {capabilities}."
    )


def test_the_manifest_exposes_anast_by_name_to_a_store_install(rendered: ET.Element) -> None:
    """The MSIX-native replacement for the installer's optional PATH task.

    A container-isolated package cannot write the machine PATH, so without this
    extension a Store install would have a CLI nobody could type.
    """
    extension = rendered.find("Applications/Application/Extensions/uap3:Extension", NS)
    assert extension is not None
    assert extension.get("Category") == "windows.appExecutionAlias"
    assert extension.get("Executable") == r"cli\anast.exe"
    assert extension.get("EntryPoint") == "Windows.FullTrustApplication"
    alias = extension.find("uap3:AppExecutionAlias/desktop:ExecutionAlias", NS)
    assert alias is not None
    assert alias.get("Alias") == "anast.exe", (
        "the alias is what an operator types; it must be spelled with its .exe "
        "suffix, which is what the extension registers."
    )


def test_the_manifest_targets_the_floor_webview2_supports(rendered: ET.Element) -> None:
    """1809 is the app's real floor: the GUI renders through WebView2, which
    supports Windows 10 1809 and later. Raising it would drop machines the
    installer already serves, for no reason this package introduces."""
    family = rendered.find("Dependencies/TargetDeviceFamily", NS)
    assert family is not None
    assert family.get("Name") == "Windows.Desktop"
    assert family.get("MinVersion") == "10.0.17763.0"
    assert family.get("MaxVersionTested") is not None


def test_the_manifest_names_only_logos_that_are_committed(
    msix: ModuleType, rendered: ET.Element
) -> None:
    visual = rendered.find("Applications/Application/uap:VisualElements", NS)
    assert visual is not None
    logo = rendered.find("Properties/Logo", NS)
    assert logo is not None and logo.text is not None
    # An MSIX manifest spells its paths with backslashes whatever the host is,
    # so the separator is split explicitly rather than through pathlib.
    named = {
        str(visual.get("Square150x150Logo")).rsplit("\\", 1)[-1],
        str(visual.get("Square44x44Logo")).rsplit("\\", 1)[-1],
        logo.text.rsplit("\\", 1)[-1],
    }
    assert named == set(msix._LOGOS), (
        "the manifest and the staging step must agree on the logo set: staging "
        f"copies {sorted(msix._LOGOS)}, the manifest names {sorted(named)}."
    )
    assert str(logo.text).startswith("Assets\\"), (
        "the manifest must point at the Assets directory staging creates"
    )


# --- the committed logo renditions -----------------------------------------


def _png_size(path: Path) -> tuple[int, int]:
    """A PNG's declared width and height, read from its IHDR chunk.

    Sixteen bytes of header rather than a Pillow import: Pillow is a dev-only
    tool here (tools/make_icons.py), not something the test suite installs.
    """
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


@pytest.mark.parametrize(
    ("name", "size"),
    [("Square150x150Logo.png", 150), ("Square44x44Logo.png", 44), ("StoreLogo.png", 50)],
)
def test_the_committed_logos_are_the_declared_sizes(name: str, size: int) -> None:
    """The filenames carry their sizes as a promise; a rendition that does not
    match the name it ships under is refused by Store submission validation,
    which is a much later place to find out."""
    assert _png_size(PACKAGING / "msix-assets" / name) == (size, size)


# --- choosing a makeappx.exe ------------------------------------------------


def _fake_sdk(root: Path, *versions: str) -> Path:
    for version in versions:
        tool = root / "bin" / version / "x64" / "makeappx.exe"
        tool.parent.mkdir(parents=True)
        tool.write_text("", encoding="utf-8")
    return root


def test_find_makeappx_takes_the_newest_installed_sdk(msix: ModuleType, tmp_path: Path) -> None:
    """Newest wins because an older packer rejects manifest elements a newer
    schema defines, and reports it as a manifest error."""
    root = _fake_sdk(tmp_path / "kits", "10.0.19041.0", "10.0.22621.0", "10.0.20348.0")
    assert msix.find_makeappx([root]).parent.parent.name == "10.0.22621.0"


def test_find_makeappx_orders_by_number_not_by_string(msix: ModuleType, tmp_path: Path) -> None:
    """A lexicographic sort would call 10.0.9999.0 newer than 10.0.22621.0."""
    root = _fake_sdk(tmp_path / "kits", "10.0.9999.0", "10.0.22621.0")
    assert msix.find_makeappx([root]).parent.parent.name == "10.0.22621.0"


def test_a_directory_that_is_not_a_version_sorts_below_every_real_one(msix: ModuleType) -> None:
    """The glob decides what is a candidate; ``sdk_version`` only decides which
    candidate is newest, so an unparseable directory name must not raise."""
    # Forward slashes so pathlib splits the components off Windows too; the
    # separator is irrelevant to what is being asserted.
    odd = Path("C:/kits/bin/NotAVersion/x64/makeappx.exe")
    real = Path("C:/kits/bin/10.0.17763.0/x64/makeappx.exe")
    assert msix.sdk_version(odd) < msix.sdk_version(real)


def test_find_makeappx_refuses_when_no_sdk_is_installed(msix: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        msix.find_makeappx([tmp_path / "kits"])
    message = str(excinfo.value)
    assert "makeappx.exe" in message
    assert str(tmp_path / "kits") in message, "the refusal must name where it looked"


# --- staging the package root ----------------------------------------------


def test_stage_refuses_a_dist_that_is_not_the_built_layout(
    msix: ModuleType, tmp_path: Path
) -> None:
    """Half a build is not a build.

    The CLI dist missing its exe means build_windows.py did not finish, and
    packing it would produce a Store submission that installs an app whose
    command line is not there.
    """
    dist = _built_dist(tmp_path)
    (dist / "anast" / "anast.exe").unlink()
    with pytest.raises(SystemExit) as excinfo:
        msix.stage_package_root(dist, tmp_path / "staged", "<Package/>")
    message = str(excinfo.value)
    assert "anast.exe" in message
    assert "build_windows.py" in message, "the refusal must name the step that was skipped"


def test_stage_lays_out_the_two_dists_the_manifest_and_the_assets(
    msix: ModuleType, tmp_path: Path
) -> None:
    staging = msix.stage_package_root(_built_dist(tmp_path), tmp_path / "staged", "<Package/>")
    assert (staging / "AppxManifest.xml").read_text(encoding="utf-8") == "<Package/>"
    # The installed layout's own directory names, so a support answer about
    # "the gui folder" is true of the installer and of the package alike.
    assert (staging / "gui" / "Anastomosis.exe").is_file()
    assert (staging / "cli" / "anast.exe").is_file()
    assert (staging / "gui" / "python312.dll").is_file(), "a dist is copied whole, not by exe"
    for logo in msix._LOGOS:
        assert (staging / "Assets" / logo).is_file()
    # The same third-party texts the installer lays down beside the app: the
    # obligation follows the bytes, not the container.
    assert (staging / "licenses" / "THIRD_PARTY_LICENSES.md").is_file()
    assert (staging / "licenses" / "APACHE-2.0.txt").is_file()
    assert (staging / "licenses" / "OFL-1.1.txt").is_file()


def test_stage_rebuilds_from_scratch_so_nothing_stale_is_packed(
    msix: ModuleType, tmp_path: Path
) -> None:
    """A file a previous run left behind would be packed into a submission
    without appearing in any build log."""
    dist = _built_dist(tmp_path)
    staging = tmp_path / "staged"
    staging.mkdir()
    (staging / "left-over.txt").write_text("from a run two versions ago", encoding="utf-8")
    msix.stage_package_root(dist, staging, "<Package/>")
    assert not (staging / "left-over.txt").exists()


# --- the makeappx invocation ------------------------------------------------


def test_makeappx_argv_packs_the_staged_root_and_never_signs(msix: ModuleType) -> None:
    """No signing flag, ever.

    The Store re-signs every package it ingests, which is the whole reason this
    artifact exists; signing here would produce a certificate nobody trusts.
    """
    argv = msix.makeappx_argv(
        Path(r"C:\kits\bin\10.0.22621.0\x64\makeappx.exe"),
        Path(r"C:\src\dist\_msix"),
        Path(r"C:\src\dist\installer\Anastomosis-0.7.0.msix"),
    )
    assert argv == [
        r"C:\kits\bin\10.0.22621.0\x64\makeappx.exe",
        "pack",
        "/o",
        "/d",
        r"C:\src\dist\_msix",
        "/p",
        r"C:\src\dist\installer\Anastomosis-0.7.0.msix",
    ]
    assert not any("sign" in part.lower() for part in argv)


# --- the workflow wiring ----------------------------------------------------
#
# Deliberately here rather than in test_release_dispatch.py: that module pins
# the dispatch-publish wiring and one cross-workflow security property, and an
# MSIX packing step is neither. Same reading style, local helpers.

# PyYAML resolves a bare ``on:`` key to the YAML 1.1 boolean True, not "on".
_ON = True


def _workflow() -> dict[Any, Any]:
    data = yaml.safe_load(WINDOWS_YML.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _step_index(steps: list[dict[str, Any]], marker: str) -> int:
    for i, step in enumerate(steps):
        if marker in f"{step.get('name', '')} {step.get('run', '')}":
            return i
    raise AssertionError(f"no step matching {marker!r}")


def test_the_msix_is_packed_from_the_build_that_already_happened() -> None:
    """One Nuitka build, two artifacts.

    Building twice would double a forty-minute job and let the installer and
    the Store package drift apart — so the packing step must come after the
    build and must not run a build of its own.
    """
    steps = _workflow()["jobs"]["build"]["steps"]
    build = _step_index(steps, "build_windows.py")
    pack = _step_index(steps, "build_msix.py")
    assert build < pack
    run = str(steps[pack].get("run", ""))
    assert "build_windows.py" not in run, "the MSIX step must not rebuild the executables"
    assert "--version" in run, "the packing step passes the version the job resolved"


def test_the_msix_is_packed_after_the_installer_has_proven_itself() -> None:
    """Same rule the SBOM follows: an artifact cut from bytes that have not
    been shown to install and run is a statement about nothing."""
    steps = _workflow()["jobs"]["build"]["steps"]
    assert _step_index(steps, "smoke_windows.py") < _step_index(steps, "build_msix.py")


def test_the_msix_is_uploaded_beside_the_installer_and_the_sbom() -> None:
    steps = _workflow()["jobs"]["build"]["steps"]
    pack = _step_index(steps, "build_msix.py")
    upload = _step_index(steps, "Upload the installer")
    assert pack < upload, "the package must exist before the step that uploads it"
    paths = steps[upload]["with"]["path"]
    for wanted in ("dist/installer/*.exe", "dist/installer/*.cdx.json", "dist/installer/*.msix"):
        assert wanted in paths, f"the CI artifact must carry {wanted}"


def test_the_msix_is_attached_to_the_github_release() -> None:
    """The release page is where a Store submission is fetched from, and where
    anyone comparing the two artifacts of one version looks."""
    steps = _workflow()["jobs"]["release"]["steps"]
    attach = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("softprops/action-gh-release")
    )
    files = attach["with"]["files"]
    for wanted in ("installer/*.exe", "installer/*.cdx.json", "installer/*.msix"):
        assert wanted in files, f"the release must carry {wanted}"


def test_the_msix_provenance_is_attested_too() -> None:
    """The package reaches the Store unsigned by design and comes back
    re-signed by Microsoft, so between this workflow and Partner Center the
    provenance attestation is the only thing that can answer who built it.
    It must be attested after it lands on the runner and before it is
    attached, same as the exe and the SBOM already are (#282, #289)."""
    steps = _workflow()["jobs"]["release"]["steps"]
    download = _step_index(steps, "Download the built installer")
    attest = _step_index(steps, "Attest build provenance")
    attach = _step_index(steps, "Attach it to the release")
    assert download < attest < attach
    subjects = steps[attest]["with"]["subject-path"]
    assert "installer/*.msix" in subjects, (
        "the MSIX must be attested alongside the exe and the SBOM; an artifact "
        "on the release page with no attestation is one nobody can check."
    )


def test_the_release_notes_say_the_msix_is_not_a_download() -> None:
    """It sits on the Releases page beside an installer, and an unsigned MSIX
    does not install — so the page has to say what it is before somebody
    double-clicks it and reads a Windows error instead."""
    steps = _workflow()["jobs"]["release"]["steps"]
    run = steps[_step_index(steps, "Extract this version's CHANGELOG section")]["run"]
    assert ".msix" in run
    assert "not a download" in run, (
        "the release notes must say plainly that the .msix is a Store "
        "submission artifact rather than something to install"
    )


def test_the_packaging_lane_still_triggers_on_its_own_sources() -> None:
    """The new files live under packaging/, which the push filter already
    covers — asserted rather than assumed, because a manifest or logo change
    that never builds is a change nobody validates."""
    paths = _workflow()[_ON]["push"]["paths"]
    assert "packaging/**" in paths
