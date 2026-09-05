"""Pins ``tools/check.sh`` <-> ``.github/workflows/ci.yml`` parity.

``tools/check.sh`` claims at the top of its file that it is "exactly what
CI runs." That parity must hold for every gate, mypy included: a gate that
lives only in check.sh with no CI lane lets a typing regression slip into
main while every author's local gate stays green. This test keeps the parity
live by parsing check.sh and ci.yml and asserting every gate command in
check.sh appears in some CI job's ``run:`` block.

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
    ("pytest", "pytest"),
    ("python tools/complexity_gate.py", "tools/complexity_gate.py"),
    ("python tools/prose_gate.py", "tools/prose_gate.py"),
    ("python tools/guard_count.py", "tools/guard_count.py"),
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


def test_pytest_is_invoked_the_same_way_in_both() -> None:
    """Same gate, same form — because the two forms do not import the same things.

    The marker match above proves a gate EXISTS in CI, not that it is invoked
    identically, and for pytest that difference is a real one: `python -m
    pytest` puts the working directory on `sys.path` and a bare `pytest` does
    not. check.sh ran the `-m` form while CI ran the bare one, so a test
    importing `tools.sbom` passed every local gate and failed five CI jobs on
    `ModuleNotFoundError` (#142). `pythonpath` in pyproject settles what is
    importable; this settles that both callers ask the same question.
    """
    # The COMMANDS, not the file text: the line above check.sh's pytest call
    # explains this very trap and names the form it avoids, and a grep over the
    # raw file cannot tell an explanation from an invocation.
    invocations = [cmd for cmd in _check_sh_commands() if "pytest" in cmd]
    assert invocations, "tools/check.sh no longer runs pytest at all"
    assert not any(cmd.startswith("python -m pytest") for cmd in invocations), (
        "tools/check.sh uses `python -m pytest`, which puts the working directory "
        "on sys.path. CI runs a bare `pytest`, which does not — so the local gate "
        "would be more permissive than the one that matters."
    )
    assert "python -m pytest" not in _ci_yml_run_blob(), (
        "ci.yml uses `python -m pytest` while tools/check.sh runs a bare `pytest`. "
        "Make them the same form, or the local gate and CI disagree about what is "
        "importable."
    )


def test_check_sh_claims_match_reality() -> None:
    """The header comment of ``tools/check.sh`` advertises the script as
    'exactly what CI runs'. With the mypy lane present in CI the claim holds;
    this assertion just makes that header comment a tested fact rather than
    aspiration."""
    header = "\n".join(CHECK_SH.read_text(encoding="utf-8").splitlines()[:5])
    assert "exactly what CI runs" in header, (
        "tools/check.sh's header comment was edited away from the parity claim. "
        "Either restore the claim (and keep the lanes in sync) or weaken the comment "
        "so it doesn't promise something CI doesn't deliver."
    )
