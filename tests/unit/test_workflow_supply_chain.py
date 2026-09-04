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


def test_upload_and_download_artifact_are_version_consistent_across_workflows() -> None:
    """``actions/upload-artifact`` and ``actions/download-artifact`` each
    appear at more than one call site (release.yml and windows-package.yml
    both write in one job and read in another), so the risk this guards is
    not upload matching download — their current majors are v7 and v8, on
    purpose, per the artifact format compatibility the vendor's own docs
    establish — it is one of the two actions getting bumped at some call
    sites and not others, the same way `.github/workflows/codeql.yml`'s
    `init`/`analyze` split once happened. Every `uses:` line for a given
    action, across every workflow file, must carry the same SHA and the same
    version comment."""
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


def test_dependabot_groups_every_update_within_each_ecosystem() -> None:
    """Every update within an ecosystem lands in ONE group, covering the whole
    ecosystem with a single `*` pattern. `open-pull-requests-limit` is what
    silently split `github/codeql-action/init` from `.../analyze` — with five
    other github-actions PRs already open, the matching `init` bump for #305's
    `analyze` bump never got opened at all. A single group cannot half-open:
    Dependabot either proposes the whole batch as one PR or none of it, so the
    limit can no longer cut a version-matched pair in half."""
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
    """hatchling is excluded from the `pip` group, and that is not a silencing.

    The backend has no `ignore` — a bare one would stop its security updates
    and a scoped one never fired against a two-sided bound — so every release
    is proposed, and the bound guard below refuses the ones measured to emit
    core-metadata 2.5. Inside the group that red is contagious: a single
    Dependabot commit lands whole or not at all, so #389's ruff floor and
    Nuitka pin sat behind a hatchling bump that could never merge. Excluding
    the backend gives it its own pull request to close by hand and leaves the
    batch mergeable, while the `*` pattern above still covers everything else
    in one PR.
    """
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
    """The Windows installer is built on a schedule and for releases, not per merge.

    A Nuitka standalone build plus the installer smoke test takes about an hour
    of windows-latest, billed at twice a Linux minute, out of the same runner
    pool every pull request's test matrix draws on. Watching
    `src/anastomosis/**` spent that on every ordinary source merge — four in
    one evening, for an artifact nobody downloaded — and the queue that built
    up was paid for by the pull requests waiting behind it.

    So the path filter watches only what decides the frozen layout, the
    nightly build is the canary for everything else, and a tag still builds
    unconditionally. Re-adding a source path here is a real decision with a
    real bill; this test is where it gets made deliberately.
    """
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
    `version-update:` one.

    Ignore conditions apply to Dependabot SECURITY updates as well as version
    updates, and this repository has no second advisory path — no pip-audit
    lane, no OSV scan, nothing that reads a published CVE. So a bare
    `- dependency-name: playwright`, written to keep routine churn out of the
    weekly batch, also stops the PR that would have told us a browser
    automation library shipped a fix for a known exploit, and stops it
    silently. Naming only `version-update:` types is what keeps the two apart:
    a security update is not a version-update type, so it still opens.

    This is the guard for a mistake that is easy to make and invisible once
    made — the config keeps working, and the thing that stopped happening
    never announces itself."""
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
    """A pip `ignore` is only defensible for a version this repo PINS itself.

    Each ignore exists because a bump needs work Dependabot cannot do:
    playwright's pin governs the rendering goldens and the bundled Chromium
    together, pywebview's governs whether the frozen Windows exe can import at
    all, and hatchling decides the core-metadata version the published wheel
    carries. An ignored name with no pin behind it is an ignore that has
    drifted off the reason it was written — routine churn suppressed for a
    package nothing was protecting.

    Both places count, and the first version of this test only knew about one.
    It read `packaging/constraints.txt` alone, so adding the hatchling ignore
    failed it — correctly, in the sense that the rule was violated, and wrongly,
    in the sense that hatchling IS pinned, just in `[build-system] requires`
    where a build backend belongs. A guard that is right about the principle
    and wrong about where to look is still wrong."""
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
    """A workflow with a `pull_request` trigger may only push-trigger on main.

    Both `ci.yml` and `codeql.yml` listed `claude/**` under `push` while also
    triggering on `pull_request`. Every branch here opens a pull request, so
    each commit fired the whole matrix twice against the same SHA — measured on
    PR #331 as run 33355245691 and run 33355224110, both green, 24 check runs
    where 12 carried all the signal.

    The duplicates are not free and not only slow. Each check run is a webhook
    and a row that anything watching the pull request then reads back, and this
    account's GitHub API quota is per-user and shared across every session and
    every repository — so a workflow that says everything twice spends someone
    else's budget to do it.

    Stated as a rule rather than a pin on two filenames, so a third workflow
    added later cannot reintroduce it quietly.
    """
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
    """`[build-system] requires` may not admit a hatchling that emits metadata 2.5.

    That line decides which backend builds the wheel, and the core-metadata
    version a hatchling release emits oscillates. The publish path refuses 2.5
    — release.yml reads Metadata-Version out of the built wheel and admits 2.1
    through 2.4 only — so a bad backend fails the release BUILD. That gate is
    ours, and it exists because the failure used to land one step later, at
    the irreversible upload: twine refused 2.5 outright on the real 0.7.0
    publish, after a tag existed and a version number had been spent. The
    twine inside the pinned publish action accepts 2.5 now; the gate stays,
    because 2.4 is what every path accepts and it carries PEP 639
    license-files identically. Either way the release workflow only runs on a
    tag, so this test is what catches a bad bound BEFORE one is spent.

    Measured by reading DEFAULT_METADATA_VERSION out of each release, most
    recently on 2026-08-31 against Dependabot's proposal of `>=1.32.0,<1.33`,
    and checked against pypa/hatch's own release history on 2026-09-03:

        1.27.0 -> 2.4    1.29.0 -> 2.4    1.30.1 -> 2.4    1.31.0 -> 2.4
        1.30.0 -> 2.5    1.32.0 -> 2.5

    Asserted against the FILE rather than against an installed hatchling, and
    that is deliberate: PEP 517 builds the backend in an isolated environment,
    so hatchling is not importable at test time on any machine or in CI. A test
    that asked the environment would skip everywhere and prove nothing — the
    exact vacuous guard this suite exists to prevent. The bound is the artifact
    a person controls, so the bound is what is checked.
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
    # The floor is load-bearing too, and in the other direction. Below 1.27,
    # hatchling parses `license-files` only in its legacy table form and
    # SILENTLY ignores the list form this project uses — producing a wheel with
    # no dist-info/licenses/ at all, which is a licence-compliance failure that
    # looks like a successful build. Asserting the bound merely contains a good
    # release does not catch a floor that has been dropped; asserting it
    # excludes a bad one does.
    assert not backend.specifier.contains(Version("1.25.0")), (
        "[build-system] requires admits a hatchling below the PEP 639 floor, "
        "which ships a wheel carrying none of the third-party licence texts it "
        "redistributes"
    )
    assert backend.specifier.contains(Version("1.29.0")), (
        "the bound has excluded a release measured good in both directions"
    )
