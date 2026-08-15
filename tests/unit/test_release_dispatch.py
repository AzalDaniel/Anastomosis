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
