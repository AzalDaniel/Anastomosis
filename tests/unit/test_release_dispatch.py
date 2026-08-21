"""Pins the release workflows' dispatch-publish wiring.

Invariant: a release is publishable straight from the Actions tab — main-only,
version-asserted — without cutting a tag by hand. Two workflows carry that
capability and this test keeps their moving parts from drifting:

  * ``windows-package.yml`` gains a boolean ``publish`` dispatch input; its
    write-scoped release job fires on a version tag OR on a dispatch that opts
    in; and the gh-release step creates tag ``v<version>`` at this run's SHA
    (``tag_name`` + ``target_commitish``) while still flagging pre-1.0 builds
    as prereleases.
  * ``release.yml`` (PyPI Trusted Publishing) is itself dispatchable — a tag
    created with GITHUB_TOKEN does not cascade-trigger its push trigger — and
    its first build step refuses a dispatch from any ref but main.

If the wiring is re-shaped, update both workflows and this test together.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WINDOWS_YML = REPO_ROOT / ".github" / "workflows" / "windows-package.yml"
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"

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
    """A mistyped `v*` tag must fail BEFORE anything is built or published.

    PyPI publishes whatever version the SOURCE carries, so without this guard
    a stale tag mints a release whose tag, artifacts, and index disagree —
    the same invariant windows-package.yml already enforces on its path.
    """
    data = _load(RELEASE_YML)
    steps = data["jobs"]["build"]["steps"]
    guard = _step_index(steps, "tag names the version")
    build = _step_index(steps, "python -m build")
    assert guard < build, "the tag/version assert must run before the build"
    run = steps[guard]["run"]
    assert "__version__" in run and "github.ref_name" in run, (
        "the guard must derive the version from the package source and compare "
        f"it against the triggering tag; its run block is {run!r}"
    )


def test_release_yml_asserts_wheel_carries_third_party_licenses() -> None:
    """The built wheel must carry the Apache-2.0 and OFL-1.1 full texts.

    The wheel redistributes the HL7 CDA stylesheet and the two GUI fonts;
    pyproject's license-files places the texts under dist-info/licenses/, and
    this workflow step is what keeps a packaging-config regression from
    shipping a wheel stripped of the attributions it owes.
    """
    data = _load(RELEASE_YML)
    steps = data["jobs"]["build"]["steps"]
    build = _step_index(steps, "python -m build")
    check = _step_index(steps, "third-party license texts")
    assert build < check, "the wheel content check must run after the build"
    run = steps[check]["run"]
    for needle in ("APACHE-2.0.txt", "OFL-1.1.txt", "THIRD_PARTY_LICENSES.md"):
        assert needle in run, f"the wheel content check must assert {needle}"
