"""Pins the advanced CodeQL setup and the inline-suppression policy:
the workflow's triggers/permissions/pinned-SHA actions, the config's
``security-extended`` suite with NO repo-wide exclusions, and every
``# codeql[...]`` suppression's own-line placement, PHI rationale, and
match against SECURITY.md's named files.

Scanned tree: ``src/`` plus the audit tools under ``docs/`` — those
run by hand against real charts, so a suppression there needs the
same audit trail as shipped code.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CODEQL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "codeql.yml"
CODEQL_CONFIG = REPO_ROOT / ".github" / "codeql" / "codeql-config.yml"
SECURITY_MD = REPO_ROOT / "SECURITY.md"
#: Every tree whose suppressions the policy governs. ``docs/`` earns its place
#: because the audit tools there read real exports; ``tests/`` does not, since a
#: fixture carries no patient.
SCAN_ROOTS = (REPO_ROOT / "src", REPO_ROOT / "docs")

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_POLICY_HEADER = "## Code scanning & suppression policy (auditable)"
# Backtick-quoted repo-root-relative Python paths inside the policy section.
_POLICY_PATH_RE = re.compile(r"`((?:src|docs)/[^`]+\.py)`")
# How many lines above a `# codeql[...]` line its rationale may sit.
_RATIONALE_WINDOW = 10
#: A suppression states one of two guarantees. PHI-BY-DESIGN: this site writes a
#: patient's own record where the operator asked for it, so the rule is reading
#: the product as a defect. PHI-FREE-BY-CONSTRUCTION: nothing sensitive reaches
#: the sink at all, and the alert is a false positive the code cannot phrase away.
_RATIONALES = ("PHI-BY-DESIGN", "PHI-FREE-BY-CONSTRUCTION")


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} did not parse to a mapping"
    return data


def _on_section(doc: dict[str, Any]) -> dict[str, Any]:
    """The workflow ``on:`` mapping, tolerating PyYAML's ``on`` -> ``True`` quirk.

    YAML 1.1 reads the bare word ``on`` as boolean true, so ``yaml.safe_load``
    keys the trigger block under ``True`` rather than the string ``"on"``.
    """
    section = doc.get("on", doc.get(True))
    assert isinstance(section, dict), "codeql.yml has no `on:` trigger mapping"
    return section


def _collect_uses(doc: dict[str, Any]) -> list[str]:
    """Every ``uses:`` string across all jobs' steps."""
    uses: list[str] = []
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []):
            ref = step.get("uses")
            if isinstance(ref, str):
                uses.append(ref)
    return uses


def _security_md_policy_files() -> set[str]:
    """The Python paths named in SECURITY.md's suppression-policy section."""
    lines = SECURITY_MD.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == _POLICY_HEADER), None)
    assert start is not None, f"SECURITY.md is missing the {_POLICY_HEADER!r} section"
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return set(_POLICY_PATH_RE.findall("\n".join(body)))


def _codeql_suppressions() -> list[tuple[Path, int, list[str]]]:
    """Every ``# codeql[`` occurrence in the scanned tree as ``(path, line, lines)``."""
    hits: list[tuple[Path, int, list[str]]] = []
    for path in sorted(q for root in SCAN_ROOTS for q in root.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines):
            if "# codeql[" in line:
                hits.append((path, idx, lines))
    return hits


# --- workflow -----------------------------------------------------------------


def test_workflow_triggers_and_permissions() -> None:
    """codeql.yml fires on push, pull request, and a schedule, and the analyze
    job carries ``security-events: write`` (required to upload SARIF)."""
    doc = _load_yaml(CODEQL_WORKFLOW)
    on_section = _on_section(doc)
    for trigger in ("push", "pull_request", "schedule"):
        assert trigger in on_section, f"codeql.yml is missing the `{trigger}` trigger"

    grants_write = any(
        job.get("permissions", {}).get("security-events") == "write"
        for job in doc.get("jobs", {}).values()
    )
    assert grants_write, "no codeql.yml job grants `security-events: write`; SARIF upload needs it."


def test_workflow_actions_are_sha_pinned() -> None:
    """Every ``uses:`` is pinned to a full 40-hex commit SHA, never a tag."""
    uses = _collect_uses(_load_yaml(CODEQL_WORKFLOW))
    assert uses, "codeql.yml declares no `uses:` steps"
    for ref in uses:
        assert "@" in ref, f"unpinned action reference {ref!r} (no `@ref`)"
        pin = ref.split("@", 1)[1]
        assert _SHA_RE.fullmatch(pin), f"action {ref!r} is not pinned to a 40-hex SHA"


# A `uses:` line together with the trailing `# vX.Y.Z` human-readable version
# comment. PyYAML discards YAML comments, so the co-versioning check below
# reads the file as text rather than through `_load_yaml`.
_USES_LINE_RE = re.compile(r"^\s*-?\s*uses:\s*(\S+)@([0-9a-f]{40})\s*#\s*(\S+)\s*$")


def _codeql_action_pins() -> dict[str, tuple[str, str]]:
    """``{"init": (sha, version), "analyze": (sha, version)}`` for the two
    ``github/codeql-action/*`` steps, read straight from the file's text."""
    pins: dict[str, tuple[str, str]] = {}
    for line in CODEQL_WORKFLOW.read_text(encoding="utf-8").splitlines():
        match = _USES_LINE_RE.match(line)
        if not match:
            continue
        ref, sha, version = match.groups()
        if ref == "github/codeql-action/init":
            pins["init"] = (sha, version)
        elif ref == "github/codeql-action/analyze":
            pins["analyze"] = (sha, version)
    return pins


def test_codeql_init_and_analyze_share_one_version() -> None:
    """``init`` and ``analyze`` are two steps of ONE CodeQL Action
    release, and the Action rejects a run where they disagree.
    Dependabot tracks each `uses:` line as an independent dependency,
    so nothing else keeps the two SHAs in sync — a property of the
    committed file, checked directly."""
    pins = _codeql_action_pins()
    assert "init" in pins and "analyze" in pins, (
        "codeql.yml must declare both a github/codeql-action/init and a "
        "github/codeql-action/analyze step, each SHA-pinned with a trailing "
        "`# vX.Y.Z` comment"
    )
    init_sha, init_version = pins["init"]
    analyze_sha, analyze_version = pins["analyze"]
    assert init_sha == analyze_sha, (
        f"codeql-action/init ({init_sha}) and codeql-action/analyze "
        f"({analyze_sha}) are pinned to different commits; the CodeQL Action "
        "refuses to run when init and analyze disagree on version."
    )
    assert init_version == analyze_version, (
        f"codeql-action/init ({init_version}) and codeql-action/analyze "
        f"({analyze_version}) carry different version comments even though "
        "their SHAs must move together — the comment is the human-readable "
        "half of the same invariant."
    )


def test_init_step_references_config_file() -> None:
    """The codeql-action init step points at the committed config, which exists."""
    doc = _load_yaml(CODEQL_WORKFLOW)
    config_refs: list[str] = []
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []):
            ref = step.get("uses", "")
            if isinstance(ref, str) and "codeql-action/init" in ref:
                config_refs.append(step.get("with", {}).get("config-file", ""))
    assert config_refs, "codeql.yml has no codeql-action/init step"
    assert config_refs == ["./.github/codeql/codeql-config.yml"], (
        f"init step config-file must be the committed config; got {config_refs!r}"
    )
    assert CODEQL_CONFIG.is_file(), "the referenced codeql-config.yml does not exist"


def test_the_suppression_mechanism_is_actually_wired_up() -> None:
    """A `# codeql[...]` comment only clears an alert if this step
    runs: the suite computes the suppression into the SARIF, and
    `advanced-security/dismiss-alerts` is what reads it back and
    dismisses the alert through the API — the other suppression tests
    check well-formedness, never that this control exists at all."""
    doc = _load_yaml(CODEQL_WORKFLOW)
    steps = [step for job in doc.get("jobs", {}).values() for step in job.get("steps", [])]

    analyze = [s for s in steps if "codeql-action/analyze" in s.get("uses", "")]
    assert len(analyze) == 1, "expected exactly one codeql-action/analyze step"
    assert analyze[0].get("id") == "analyze", (
        "the analyze step needs an `id` so the dismissal step can name its upload"
    )
    output = analyze[0].get("with", {}).get("output")
    assert output, "the analyze step needs `output:` so the SARIF exists on disk to be read"

    init = [s for s in steps if "codeql-action/init" in s.get("uses", "")]
    assert len(init) == 1, "expected exactly one codeql-action/init step"
    packs = str(init[0].get("with", {}).get("packs", ""))
    assert "AlertSuppression.ql" in packs, (
        "init does not ask for the alert-suppression query, so the CodeQL CLI never "
        "computes the SARIF's `suppressions[]` property and the dismissal step below "
        "has nothing to match: every `# codeql[...]` comment in this repository would "
        "be decoration again. Merging #310 proved this — the step ran green, indexed "
        "nine alerts and dismissed none."
    )

    dismiss = [s for s in steps if "dismiss-alerts" in s.get("uses", "")]
    assert len(dismiss) == 1, (
        f"expected exactly one dismissal step, found {len(dismiss)}: every "
        "`# codeql[...]` suppression in this repository is decoration without "
        "one, and multiple mutators widen the alert-state trust boundary"
    )
    inputs = dismiss[0].get("with", {})
    assert inputs.get("sarif-id") == "${{ steps.analyze.outputs.sarif-id }}", (
        "the dismissal step must take the sarif-id of the upload it is dismissing from"
    )
    assert str(inputs.get("sarif-file", "")).startswith(f"{output}/"), (
        f"the dismissal step reads {inputs.get('sarif-file')!r}, which is not in the "
        f"{output!r} directory the analyze step writes"
    )
    guard = str(dismiss[0].get("if", ""))
    assert guard == "github.event_name == 'push' && github.ref == 'refs/heads/main'", (
        "the dismissal step mutates repository alert state with security-events: write; "
        "it must run only for accepted code pushed to main, never from a pull request, "
        "feature branch, or scheduled scan"
    )


# --- config -------------------------------------------------------------------


def test_config_selects_extended_suite() -> None:
    """The config runs security-extended, whose built-in AlertSuppression.ql
    query honors inline ``# codeql[...]`` comments with no extra pack."""
    config = _load_yaml(CODEQL_CONFIG)
    query_suites = {q.get("uses") for q in config.get("queries", []) if isinstance(q, dict)}
    assert "security-extended" in query_suites, "config must select the security-extended suite"


def test_config_declares_no_repo_wide_exclusions() -> None:
    """No ``query-filters`` and no ``paths`` / ``paths-ignore``: coverage is
    full-tree, and exclusion is deliberately inline-per-site only."""
    config = _load_yaml(CODEQL_CONFIG)
    for forbidden in ("query-filters", "paths", "paths-ignore"):
        assert forbidden not in config, (
            f"codeql-config.yml declares `{forbidden}`, a repo-wide exclusion. The policy is "
            "full-tree coverage with per-site inline suppressions only."
        )


# --- inline suppressions ------------------------------------------------------


def test_every_suppression_is_alone_and_justified() -> None:
    """Each ``# codeql[...]`` sits alone on its line (CodeQL only honors it there)
    and states one of the two rationales within the lines just above it."""
    hits = _codeql_suppressions()
    assert hits, "expected inline `# codeql[...]` suppressions, found none"
    for path, idx, lines in hits:
        rel = path.relative_to(REPO_ROOT).as_posix()
        assert lines[idx].strip().startswith("# codeql["), (
            f"{rel}:{idx + 1} — a `# codeql[...]` suppression must be alone on its own line; "
            "CodeQL ignores it when it trails code."
        )
        following = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
        assert following and not following.startswith("#"), (
            f"{rel}:{idx + 1} — a suppression covers ONLY the line immediately below it; "
            "a blank or comment line there silently un-suppresses the site."
        )
        window = lines[max(0, idx - _RATIONALE_WINDOW) : idx]
        assert any(tag in line for line in window for tag in _RATIONALES), (
            f"{rel}:{idx + 1} — no {' or '.join(_RATIONALES)} rationale within the "
            f"{_RATIONALE_WINDOW} lines above the suppression."
        )


def test_suppression_files_match_security_policy() -> None:
    """The set of files carrying suppressions equals, in both directions, the set
    SECURITY.md's suppression-policy section names — so a suppression cannot land
    without amending the policy, and a policy entry cannot outlive its site."""
    documented = _security_md_policy_files()
    found = {
        path.relative_to(REPO_ROOT).as_posix() for path, _idx, _lines in _codeql_suppressions()
    }
    assert found == documented, (
        "inline-suppression files and SECURITY.md's policy list disagree.\n"
        f"  suppressed but undocumented: {sorted(found - documented)}\n"
        f"  documented but unsuppressed: {sorted(documented - found)}"
    )
