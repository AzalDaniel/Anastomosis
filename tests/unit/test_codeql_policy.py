"""Pins the advanced CodeQL setup and the inline-suppression policy.

Three invariants live here, each a property of the committed files (not a
record of how they came to be):

* The advanced workflow (``.github/workflows/codeql.yml``) triggers on push,
  pull request, and a schedule; the analyzing job holds ``security-events:
  write`` so its SARIF can upload; and every third-party action is pinned to a
  full 40-hex commit SHA (never a floating tag).
* The config (``.github/codeql/codeql-config.yml``) selects the
  ``security-extended`` suite (which ships the built-in ``AlertSuppression.ql``
  query that honors inline ``# codeql[...]`` comments — no extra pack needed)
  and declares NO repo-wide exclusions — no ``query-filters`` and no
  ``paths`` / ``paths-ignore``. Coverage is full-tree; suppression is per-site.
* Every ``# codeql[...]`` suppression in ``src/`` sits alone on its own line
  (CodeQL only honors it there), carries a ``PHI-BY-DESIGN`` rationale within the
  lines just above it, and lives in a file that SECURITY.md's "Code scanning &
  suppression policy (auditable)" section names — with the set matching exactly
  in both directions, so a new suppression cannot land without amending the
  policy and a retired policy entry cannot outlive its suppression.
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
SRC_ROOT = REPO_ROOT / "src"

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_POLICY_HEADER = "## Code scanning & suppression policy (auditable)"
# Backtick-quoted repo-root-relative Python paths inside the policy section.
_SRC_PATH_RE = re.compile(r"`(src/[^`]+\.py)`")
# How many lines above a `# codeql[...]` line the PHI-BY-DESIGN rationale may sit.
_RATIONALE_WINDOW = 8


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
    """The ``src/...py`` paths named in SECURITY.md's suppression-policy section."""
    lines = SECURITY_MD.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == _POLICY_HEADER), None)
    assert start is not None, f"SECURITY.md is missing the {_POLICY_HEADER!r} section"
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return set(_SRC_PATH_RE.findall("\n".join(body)))


def _codeql_suppressions() -> list[tuple[Path, int, list[str]]]:
    """Every ``# codeql[`` occurrence in ``src/`` as ``(path, line_index, lines)``."""
    hits: list[tuple[Path, int, list[str]]] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
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
    """``init`` and ``analyze`` are two steps of ONE CodeQL Action release, and
    the Action rejects a run where they disagree. Dependabot tracks each
    `uses:` line as an independent dependency — that gap is exactly how a
    weekly batch once bumped `analyze` alone and left `init` a version
    behind, because the ecosystem's PR limit was already spent on other
    updates by the time `init`'s turn came. This is a property of the
    committed file, checked directly, rather than something inferred from how
    the two pins arrived here."""
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
    """A `# codeql[...]` comment only clears an alert if this step runs.

    The suite computes the suppression into the SARIF and code scanning ignores
    it; `advanced-security/dismiss-alerts` is what reads it back and dismisses
    the alert through the API. Six suppressions sat in `src/` doing nothing
    because that step was missing, so the tests below — which check that every
    suppression is well formed and documented — were all passing over a control
    that did not exist. This is the one that would have caught it.
    """
    doc = _load_yaml(CODEQL_WORKFLOW)
    steps = [step for job in doc.get("jobs", {}).values() for step in job.get("steps", [])]

    analyze = [s for s in steps if "codeql-action/analyze" in s.get("uses", "")]
    assert len(analyze) == 1, "expected exactly one codeql-action/analyze step"
    assert analyze[0].get("id") == "analyze", (
        "the analyze step needs an `id` so the dismissal step can name its upload"
    )
    output = analyze[0].get("with", {}).get("output")
    assert output, "the analyze step needs `output:` so the SARIF exists on disk to be read"

    dismiss = [s for s in steps if "dismiss-alerts" in s.get("uses", "")]
    assert dismiss, (
        "no dismissal step: every `# codeql[...]` suppression in this repository "
        "is decoration without it, and SECURITY.md claims otherwise."
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
    assert "fork" in guard or "head.repo" in guard, (
        "the dismissal step holds security-events: write and must not run for a fork's PR"
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
    and has a ``PHI-BY-DESIGN`` rationale within the lines just above it."""
    hits = _codeql_suppressions()
    assert hits, "expected inline `# codeql[...]` suppressions in src/, found none"
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
        assert any("PHI-BY-DESIGN" in line for line in window), (
            f"{rel}:{idx + 1} — no PHI-BY-DESIGN rationale within the "
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
