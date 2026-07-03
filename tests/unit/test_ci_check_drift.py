# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""Pins ``tools/check.sh`` <-> ``.github/workflows/ci.yml`` parity.

``tools/check.sh`` claims at the top of its file that it is "exactly what
CI runs." Pre-PR-R, mypy lived in check.sh but had no CI lane — silent
drift, the kind that lets a typing regression slip into main while every
PR author's local gate stays green. PR-R adds the mypy lane; this test
keeps the parity live by parsing check.sh and ci.yml and asserting every
gate command in check.sh appears in some CI job's ``run:`` block.

When the gate roster legitimately changes (a new check.sh command, or a
deliberate retirement), update both files in the same PR and the test
keeps passing.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_SH = REPO_ROOT / "tools" / "check.sh"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The exact gate commands ``tools/check.sh`` runs. Each maps to a marker
# substring the test expects to find inside SOME CI ``run:`` step. We use
# a marker (not the full command) because CI usually invokes the same gate
# with extra flags (``pytest -m e2e``, ``pytest tests/integration -m ...``,
# ``pip install ruff && ruff check`` chained, etc.). The marker is the
# minimum string CI must contain for the gate to count as covered.
_GATE_MARKERS: tuple[tuple[str, str], ...] = (
    ("python tools/preflight.py", "tools/preflight.py"),
    ("ruff check .", "ruff check"),
    ("ruff format --check .", "ruff format --check"),
    ("python -m mypy", "mypy"),  # CI may use bare `mypy` or `python -m mypy`
    ("python -m pytest", "pytest"),
    ("python tools/phi_scan.py", "tools/phi_scan.py"),
)


def _check_sh_commands() -> set[str]:
    """The set of gate command strings declared in ``tools/check.sh``.

    Skips comments and the bash boilerplate (``set``, ``cd``, ``echo``);
    everything else is a gate that needs CI representation.
    """
    cmds: set[str] = set()
    for raw in CHECK_SH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("set ", "cd ", "echo ", "#!")):
            continue
        cmds.add(line)
    return cmds


def _ci_yml_run_blob() -> str:
    """All CI ``run:`` step bodies concatenated into one blob.

    We don't need to parse YAML structure for the parity check: any gate
    command appearing as a substring of any ``run:`` line is by definition
    a CI invocation of that gate. Keeping it string-based avoids a PyYAML
    dependency in the test suite.
    """
    text = CI_YML.read_text(encoding="utf-8")
    runs: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("- run:"):
            runs.append(stripped[len("- run:") :])
        elif stripped.startswith("run:"):
            runs.append(stripped[len("run:") :])
    return "\n".join(runs)


def test_every_check_sh_gate_has_a_ci_lane() -> None:
    declared = _check_sh_commands()
    # Every line in check.sh that we expect to map to CI is declared in
    # _GATE_MARKERS — fail loudly if check.sh grew a new gate the test
    # doesn't know about yet (the test itself becomes the drift signal).
    expected_in_check_sh = {decl for decl, _marker in _GATE_MARKERS}
    extra = declared - expected_in_check_sh
    assert not extra, (
        f"tools/check.sh has gate commands this drift test does not know about: {sorted(extra)}. "
        "Add them to _GATE_MARKERS in tests/unit/test_ci_check_drift.py with the marker "
        "substring CI uses for that gate, and add a CI lane that runs them."
    )

    ci_blob = _ci_yml_run_blob()
    missing: list[str] = []
    for decl, marker in _GATE_MARKERS:
        if marker not in ci_blob:
            missing.append(f"{decl!r} (marker {marker!r}) is in tools/check.sh but no CI run: step")
    assert not missing, (
        "tools/check.sh claims to be 'exactly what CI runs' but these gates are missing from "
        ".github/workflows/ci.yml:\n  " + "\n  ".join(missing)
    )


def test_check_sh_claims_match_reality() -> None:
    """The header comment of ``tools/check.sh`` advertises the script as
    'exactly what CI runs'. PR-R closed the mypy gap so the claim is now
    true; this assertion just makes that header comment a tested fact
    rather than aspiration."""
    header = "\n".join(CHECK_SH.read_text(encoding="utf-8").splitlines()[:5])
    assert "exactly what CI runs" in header, (
        "tools/check.sh's header comment was edited away from the parity claim. "
        "Either restore the claim (and keep the lanes in sync) or weaken the comment "
        "so it doesn't promise something CI doesn't deliver."
    )
