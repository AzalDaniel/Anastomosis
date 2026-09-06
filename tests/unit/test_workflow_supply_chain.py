"""Pins the CI supply-chain hardening posture.

Every action, first-party included, is pinned to a full 40-hex commit SHA
(a major-version tag is a moving target; a trailing ``# vX`` comment names
it for humans). Release/publish jobs run under a dedicated GitHub
Environment (``release``/``pypi``), unreachable from a build/test job. Both
Dependabot ecosystems (``github-actions``, ``pip``) carry a cooldown so a
release cut yesterday cannot reach us today.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
DEPENDABOT_YML = REPO_ROOT / ".github" / "dependabot.yml"
CONSTRAINTS = REPO_ROOT / "packaging" / "constraints.txt"
PYPROJECT = REPO_ROOT / "pyproject.toml"
WINDOWS_YML = WORKFLOW_DIR / "windows-package.yml"
RELEASE_YML = WORKFLOW_DIR / "release.yml"

# A `uses:` reference to a public marketplace action: owner/repo[/subpath]@ref.
# Local composite actions (`uses: ./...`) are a different trust boundary —
# they run source already reviewed as part of this repo — and are excluded.
_USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)@(\S+)\s*(?:#.*)?$")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Same shape as `_USES_RE`, but also captures the trailing `# vX.Y.Z` comment
# — PyYAML has no notion of it, so the co-versioning check below reads it
# straight off the line rather than through `_load`.
_USES_WITH_VERSION_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)@(\S+)\s*#\s*(\S+)\s*$")

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
    """No `uses:` line — first-party `actions/*` included — rides a tag: a
    version tag can be force-moved by the action's own maintainers; only a
    commit SHA is immutable."""
    offenders: list[str] = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        for ref, pin in _uses_refs(path):
            if not _FULL_SHA_RE.match(pin):
                offenders.append(f"{path.name}: {ref}@{pin}")
    assert not offenders, (
        "these actions are not pinned to a full 40-hex commit SHA (a tag or "
        f"branch ref can move out from under us): {offenders}"
    )


def test_upload_and_download_artifact_are_version_consistent_across_workflows() -> None:
    """Every `uses:` line for `actions/upload-artifact` and
    `actions/download-artifact`, across every workflow file, must carry the
    same SHA and version comment. The risk is not upload/download mismatched
    majors (v7/v8 is correct, per the vendor's own compatibility docs) — it
    is one call site bumped and another not."""
    pins: dict[str, set[tuple[str, str]]] = {"upload": set(), "download": set()}
    action_names = {
        "actions/upload-artifact": "upload",
        "actions/download-artifact": "download",
    }
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _USES_WITH_VERSION_RE.match(line)
            if not match:
                continue
            ref, pin, version = match.groups()
            key = action_names.get(ref)
            if key is not None:
                pins[key].add((pin, version))

    call_sites = (
        ("upload", "actions/upload-artifact"),
        ("download", "actions/download-artifact"),
    )
    for key, action_name in call_sites:
        found = pins[key]
        assert found, f"no `{action_name}` `uses:` line found across .github/workflows/*.yml"
        assert len(found) == 1, (
            f"`{action_name}` is pinned to more than one (SHA, version) pair across "
            f"the workflows, so some call site was bumped and another was not: {found}"
        )


def test_no_stale_first_party_tags_convention_comment() -> None:
    """The retired 'first-party actions/* stay on major tags' convention
    must not creep back into a comment beside a pin that contradicts it."""
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


def test_dependabot_groups_every_update_within_each_ecosystem() -> None:
    """Every update within an ecosystem lands in ONE group via a `*`
    pattern: `open-pull-requests-limit` can otherwise silently split a
    version-matched pair into separate PRs (#305). A single group proposes
    the whole batch or none of it."""
    data = _load(DEPENDABOT_YML)
    updates = data.get("updates") or []
    by_ecosystem = {u.get("package-ecosystem"): u for u in updates}
    for ecosystem in ("github-actions", "pip"):
        groups = by_ecosystem[ecosystem].get("groups") or {}
        assert groups, f"dependabot.yml's `{ecosystem}` block declares no `groups:`"
        patterns = {p for group in groups.values() for p in group.get("patterns", [])}
        assert "*" in patterns, (
            f"dependabot.yml's `{ecosystem}` block must have a group matching `*` "
            "so every update in the ecosystem lands in one PR, not just some of them."
        )


def test_the_build_backend_does_not_ride_with_the_rest_of_the_pip_batch() -> None:
    """hatchling is excluded from the `pip` group so a release the bound
    below refuses cannot block the whole batch (#389): it still gets
    proposed, in its own pull request, while `*` still covers everything
    else in one."""
    updates = _load(DEPENDABOT_YML).get("updates") or []
    pip = next(u for u in updates if u.get("package-ecosystem") == "pip")
    groups = pip.get("groups") or {}
    excluded = {p for group in groups.values() for p in group.get("exclude-patterns", [])}
    assert "hatchling" in excluded, (
        "dependabot.yml's `pip` group no longer excludes hatchling, so a bump the "
        "build-backend guard refuses would again block every other pip update in "
        "the same batch."
    )


def test_the_installer_lane_does_not_rebuild_on_every_source_merge() -> None:
    """The Windows installer builds on a schedule and for a release tag, not
    per source merge: the Nuitka build plus installer smoke test costs about
    an hour of windows-latest, the scarcest runner class, shared with every
    pull request's own test matrix. Re-adding `src/anastomosis/**` to the
    path filter here is a deliberate decision to make, not a side effect."""
    triggers = _load(WINDOWS_YML)[_ON]
    push = triggers["push"]
    assert "src/anastomosis/**" not in push.get("paths", []), (
        "windows-package.yml watches src/anastomosis/** again, so every source "
        "merge rebuilds the installer — about an hour of windows-latest each. If "
        "that is wanted, say why here and in the workflow's own comment."
    )
    assert push.get("tags") == ["v*"], (
        "windows-package.yml must still build unconditionally for a release tag: "
        "the schedule is a canary, not the release path."
    )
    assert triggers.get("schedule"), (
        "windows-package.yml no longer builds on a schedule, so a source change "
        "that only breaks the frozen build has nothing left watching for it."
    )


def _ignore_conditions(ecosystem: str) -> list[dict[str, Any]]:
    updates = _load(DEPENDABOT_YML).get("updates") or []
    block = next(u for u in updates if u.get("package-ecosystem") == ecosystem)
    return list(block.get("ignore") or [])


def test_no_ignore_condition_silences_a_security_update() -> None:
    """An `ignore` entry must name `update-types`, and every type must be a
    `version-update:` one: this repo has no other advisory path (no
    pip-audit lane, no OSV scan), so a bare ignore also silences that
    dependency's SECURITY updates, invisibly."""
    for ecosystem in ("github-actions", "pip"):
        for condition in _ignore_conditions(ecosystem):
            name = condition.get("dependency-name", "<unnamed>")
            types = condition.get("update-types")
            assert types, (
                f"dependabot.yml's `{ecosystem}` ignore for {name!r} names no "
                "`update-types`, so it silences that dependency's SECURITY "
                "updates too. Name the `version-update:` types to stop routine "
                "bumps only."
            )
            for kind in types:
                assert str(kind).startswith("version-update:"), (
                    f"dependabot.yml's `{ecosystem}` ignore for {name!r} lists "
                    f"{kind!r}, which is not a `version-update:` type and can "
                    "therefore reach a security update."
                )


def test_every_ignored_pip_dependency_is_actually_pinned() -> None:
    """A pip `ignore` is defensible only for a version this repo pins
    itself: playwright's governs the rendering goldens, pywebview's whether
    the frozen Windows exe imports at all, hatchling's the wheel's
    core-metadata version. Checked against both `packaging/constraints.txt`
    and `[build-system] requires`, since a build backend is pinned there."""
    import tomllib

    pinned = {
        line.split("==")[0].strip().lower()
        for line in CONSTRAINTS.read_text().splitlines()
        if "==" in line and not line.lstrip().startswith("#")
    }
    pinned |= {
        entry.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip().lower()
        for entry in tomllib.loads(PYPROJECT.read_text())["build-system"]["requires"]
    }
    for condition in _ignore_conditions("pip"):
        name = str(condition.get("dependency-name", "")).lower()
        assert name in pinned, (
            f"dependabot.yml ignores pip updates for {name!r}, but "
            "packaging/constraints.txt does not pin it. An ignore with no pin "
            "behind it suppresses updates for no stated reason."
        )


def test_no_workflow_runs_twice_for_one_commit() -> None:
    """A workflow with a `pull_request` trigger may only push-trigger on
    main: every branch here opens a pull request, so any other push branch
    fires the whole matrix twice against the same SHA, doubling the check
    runs this account's shared GitHub API quota serves (#331). A rule, not
    a pin on two filenames."""
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        triggers = _load(path).get(True) or _load(path).get("on") or {}
        if not isinstance(triggers, dict) or "pull_request" not in triggers:
            continue
        push = triggers.get("push") or {}
        branches = list(push.get("branches") or []) if isinstance(push, dict) else []
        extra = [b for b in branches if b != "main"]
        assert not extra, (
            f"{path.name} triggers on `pull_request` AND pushes to {extra}; every "
            "branch here opens a pull request, so each commit would run this "
            "workflow twice against the same SHA. Push-trigger on main only."
        )


def test_the_build_backend_bound_excludes_the_measured_bad_releases() -> None:
    """Contract: `[build-system] requires` must exclude every hatchling
    release measured to emit core-metadata 2.5 (1.30.0, 1.32.0) — release.yml
    admits only 2.1-2.4 out of the built wheel, so a bad backend fails the
    release build before the irreversible publish upload — and must keep the
    PEP 639 floor (>=1.27, excluding 1.25.0). Checked against the file, not
    an installed hatchling: PEP 517 builds the backend in an isolated
    environment, so it is not importable at test time.

    Measured release -> metadata-version, most recently against pypa/hatch's
    own history: 1.27.0/1.29.0/1.30.1/1.31.0 -> 2.4; 1.30.0/1.32.0 -> 2.5.
    """
    import tomllib

    from packaging.requirements import Requirement
    from packaging.version import Version

    requires = tomllib.loads(PYPROJECT.read_text())["build-system"]["requires"]
    backend = next(r for r in (Requirement(entry) for entry in requires) if r.name == "hatchling")
    for bad in ("1.30.0", "1.32.0"):
        assert not backend.specifier.contains(Version(bad)), (
            f"[build-system] requires admits hatchling {bad}, which emits "
            "core-metadata 2.5 and fails the publish upload"
        )
    # Below 1.27, hatchling silently drops `license-files` in list form,
    # producing a wheel with no dist-info/licenses/ — a compliance failure
    # that looks like a successful build.
    assert not backend.specifier.contains(Version("1.25.0")), (
        "[build-system] requires admits a hatchling below the PEP 639 floor, "
        "which ships a wheel carrying none of the third-party licence texts it "
        "redistributes"
    )
    assert backend.specifier.contains(Version("1.29.0")), (
        "the bound has excluded a release measured good in both directions"
    )
