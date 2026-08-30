"""Pins the CI supply-chain hardening posture: float-free action pins, secret
isolation between build and release, and a Dependabot cooldown.

Invariant, replacing the older "third-party pinned by SHA, first-party stays
on major tags" convention: EVERY action this repo runs — ``actions/checkout``
and the rest of first-party ``actions/*`` included — is pinned to a full
40-hex commit SHA. A major-version tag (``v6``, ``v4``...) is a moving
target a maintainer can repoint at will; only a commit SHA is immutable. A
trailing ``# vX`` comment still names the version for humans.

It also pins the secret-isolation boundary this hardening pass drew: the
jobs that attach a release or publish a package run under a dedicated GitHub
Environment (``release`` / ``pypi``), so whatever a future signing step needs
is never reachable from a build/test job — and the per-push build jobs that
must stay outside that boundary (they run project code, or run on every
ordinary push) carry no environment at all.

And it pins the Dependabot config: both ecosystems this repo depends on
(``github-actions``, ``pip``) are covered with a cooldown, so a release cut
yesterday cannot reach us today.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
DEPENDABOT_YML = REPO_ROOT / ".github" / "dependabot.yml"
WINDOWS_YML = WORKFLOW_DIR / "windows-package.yml"
RELEASE_YML = WORKFLOW_DIR / "release.yml"

# A `uses:` reference to a public marketplace action: owner/repo[/subpath]@ref.
# Local composite actions (`uses: ./...`) are a different trust boundary —
# they run source already reviewed as part of this repo — and are excluded.
_USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)@(\S+)\s*(?:#.*)?$")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# PyYAML resolves the bare ``on:`` key to the YAML 1.1 boolean True, not the
# string "on" (see test_release_dispatch.py's `_ON`).
_ON = True


def _load(path: Path) -> dict[Any, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} did not parse to a mapping"
    return data


def _uses_refs(path: Path) -> list[tuple[str, str]]:
    """Every ``(ref, pin)`` pair from a ``uses:`` line in a workflow file."""
    refs: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _USES_RE.match(line)
        if not match:
            continue
        ref, pin = match.group(1), match.group(2)
        if ref.startswith("."):
            continue  # local composite action: not a marketplace pin.
        refs.append((ref, pin))
    return refs


def test_every_workflow_parses() -> None:
    # A malformed workflow fails silently in the Actions UI, not in review.
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        _load(path)


def test_every_action_pinned_to_a_full_commit_sha() -> None:
    """No `uses:` line — first-party `actions/*` included — rides a tag.

    A version tag such as `v6` can be force-moved by the action's own
    maintainers to point at different code without a single line of this
    repo changing; a commit SHA cannot.
    """
    offenders: list[str] = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        for ref, pin in _uses_refs(path):
            if not _FULL_SHA_RE.match(pin):
                offenders.append(f"{path.name}: {ref}@{pin}")
    assert not offenders, (
        "these actions are not pinned to a full 40-hex commit SHA (a tag or "
        f"branch ref can move out from under us): {offenders}"
    )


def test_no_stale_first_party_tags_convention_comment() -> None:
    """Regression guard: the old convention this repo used to document —
    'first-party actions/* stay on major tags' — must not creep back in
    alongside a pin that no longer follows it."""
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        assert "stay on major tags" not in text, (
            f"{path.name} still documents the retired 'first-party stays on "
            "major tags' convention, but every action here is now pinned to "
            "a full commit SHA — the comment is stale and must be rewritten."
        )


def test_windows_release_job_runs_under_an_environment() -> None:
    """The write-scoped job that attaches installers to a GitHub release is
    isolated behind `environment: release` — the boundary a future signing
    secret would live inside."""
    data = _load(WINDOWS_YML)
    assert data["jobs"]["release"].get("environment") == "release", (
        "windows-package.yml's `release` job must declare `environment: "
        "release` so a future signing secret is reachable only from here, "
        "never from the `build` job that runs on every ordinary push."
    )


def test_windows_build_job_carries_no_environment() -> None:
    """The build job runs on every push to main/claude branches — it must
    never be able to read what the release environment holds."""
    data = _load(WINDOWS_YML)
    assert "environment" not in data["jobs"]["build"], (
        "windows-package.yml's `build` job must not declare an `environment`: "
        "it runs on every ordinary push, and secret isolation from the "
        "`release` environment is the point."
    )


def test_release_yml_publish_job_runs_under_an_environment() -> None:
    """The PyPI publish job is isolated behind its own environment. It keeps
    the name `pypi` rather than `release` because PyPI's Trusted Publisher
    configuration binds to that exact environment name — renaming it would
    silently break publishing."""
    data = _load(RELEASE_YML)
    env = data["jobs"]["publish"].get("environment")
    assert isinstance(env, dict) and env.get("name") == "pypi", (
        "release.yml's `publish` job must run under `environment: {name: "
        f"pypi, ...}}`; found {env!r}."
    )


def test_release_yml_build_job_carries_no_environment() -> None:
    """The build job runs project code (`python -m build`); PyPA's own
    build/publish split exists to keep the code-executing job away from
    anything credentialed, so it must stay outside the `pypi` environment."""
    data = _load(RELEASE_YML)
    assert "environment" not in data["jobs"]["build"], (
        "release.yml's `build` job must not declare an `environment`: it "
        "runs project code, and the build/publish split is what keeps that "
        "job away from credentials that only `publish` should reach."
    )


def test_dependabot_config_exists_and_parses() -> None:
    assert DEPENDABOT_YML.exists(), ".github/dependabot.yml is missing"
    data = _load(DEPENDABOT_YML)
    assert data.get("version") == 2


def test_dependabot_covers_both_ecosystems_with_a_cooldown() -> None:
    """`github-actions` and `pip` are the only two ecosystems this repo
    resolves dependencies through, and both must cool down before a fresh
    release can reach us — 7 days, chosen deliberately above GitHub's 3-day
    default for a project whose product is medical-record handling."""
    data = _load(DEPENDABOT_YML)
    updates = data.get("updates") or []
    by_ecosystem = {u.get("package-ecosystem"): u for u in updates}
    for ecosystem in ("github-actions", "pip"):
        assert ecosystem in by_ecosystem, (
            f"dependabot.yml has no `{ecosystem}` update block; both "
            "github-actions and pip pins must be covered."
        )
        block = by_ecosystem[ecosystem]
        cooldown = block.get("cooldown") or {}
        default_days = cooldown.get("default-days")
        assert isinstance(default_days, int) and default_days >= 7, (
            f"dependabot.yml's `{ecosystem}` block must set "
            f"cooldown.default-days >= 7; found {default_days!r}."
        )
        assert block.get("schedule", {}).get("interval"), (
            f"dependabot.yml's `{ecosystem}` block must declare a schedule interval."
        )
