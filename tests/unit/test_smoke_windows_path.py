r"""What the Windows installer is allowed to do to the machine PATH.

The shipped installer appended the CLI directory to the machine PATH again on
every upgrade and left one segment behind on uninstall (#281). Its idempotence
Check compared the literal ``{app}\cli`` — Inno does not expand constants in a
Check function's string parameter — against a PATH holding the expanded
directory, so the check answered "not there yet" every single time.

The fix is in ``packaging/anastomosis.iss`` and is proved end to end by the
Windows smoke job, which installs the real installer four times and counts the
real registry PATH after each step. What can be tested here is the half of that
step written in Python: what counts as a segment the installer owns, what the
count matrix accepts, and what the preservation check calls damage. Get the
ownership test wrong in either direction and the smoke either misses the
duplicate it exists to catch or accuses the installer of eating a directory
that was never its to touch.
"""

from __future__ import annotations

import importlib.util
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SMOKE = Path(__file__).resolve().parents[2] / "packaging" / "smoke_windows.py"

#: The directory anastomosis.iss puts on PATH, spelled the way it writes it.
_DIR = r"C:\Program Files\Anastomosis\cli"


@pytest.fixture(scope="module")
def smoke() -> ModuleType:
    """Load the smoke script by path — it is a packaging script, not a module
    of the installed package, and importing it must not require Windows."""
    spec = importlib.util.spec_from_file_location("smoke_windows", _SMOKE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_path_keeps_its_empty_segments(smoke: ModuleType) -> None:
    """A PATH that ends in ';' must still end in ';' when the installer is done
    with it, so the empties are part of what gets compared."""
    assert smoke._path_segments("A;;B;") == ["A", "", "B", ""]


@pytest.mark.parametrize(
    "lookalike",
    [
        _DIR + "2",  # a sibling whose name starts with ours
        _DIR + r"\bin",  # a child of ours
        r"C:\Program Files\Anastomosis",  # our parent
        _DIR[:-1],  # a prefix of ours
    ],
)
def test_a_lookalike_segment_is_not_ours(smoke: ModuleType, lookalike: str) -> None:
    """The whole-segment compare. A substring or prefix test would hand every
    one of these to the uninstaller to delete out of somebody's machine PATH."""
    assert smoke._owns(lookalike, _DIR) is False
    assert smoke._count_owned(f"{_DIR};{lookalike}", _DIR) == 1
    assert smoke._unowned(f"{_DIR};{lookalike}", _DIR) == [lookalike]


def test_our_segment_is_ours_however_it_is_cased(smoke: ModuleType) -> None:
    """Windows paths are case-insensitive, so a case-sensitive count would read
    a duplicate the user's shell resolves as one directory as zero of ours."""
    value = ";".join([_DIR.upper(), _DIR.lower(), _DIR])
    assert smoke._count_owned(value, _DIR) == 3


def test_a_trailing_backslash_is_not_a_segment_we_may_delete(smoke: ModuleType) -> None:
    """The removal side is spelling-exact on purpose: the installer writes the
    directory without a trailing separator, so a segment carrying one was put
    there by somebody else and stays. (The .iss presence test is the tolerant
    half of the policy — it declines to ADD a second segment when the user
    already spelled it that way.)"""
    assert smoke._count_owned(_DIR + "\\", _DIR) == 0


def test_the_unowned_remainder_survives_the_append(smoke: ModuleType) -> None:
    """What a correct install does to PATH: one segment more, nothing else
    touched — including the empty one a trailing ';' leaves behind."""
    before = r"C:\Windows;;C:\Windows\System32;"
    after = f"{before};{_DIR}"
    assert smoke._preservation_problems(before, after, _DIR) == []
    assert smoke._count_owned(after, _DIR) == 1


def test_a_dropped_user_segment_is_named(smoke: ModuleType) -> None:
    before = f"{_DIR}2;C:\\Windows;{_DIR}"
    after = "C:\\Windows"
    problems = smoke._preservation_problems(before, after, _DIR)
    assert len(problems) == 1
    assert "removed PATH segments it does not own" in problems[0]
    assert f"{_DIR}2" in problems[0]


def test_a_reordered_path_is_damage_too(smoke: ModuleType) -> None:
    """Nothing is lost or gained here, and PATH still means something different
    afterwards: order decides which of two `anast.exe` a shell finds."""
    problems = smoke._preservation_problems("A;B", "B;A", _DIR)
    assert len(problems) == 1
    assert "changed order or repetition" in problems[0]


def test_moving_only_our_own_segments_is_not_damage(smoke: ModuleType) -> None:
    assert smoke._preservation_problems(f"A;{_DIR};B", "A;B", _DIR) == []


def test_the_matrix_accepts_the_fixed_installer(smoke: ModuleType) -> None:
    assert smoke._matrix_problems([0, 1, 1, 1, 0]) == []


def test_the_matrix_names_every_step_the_shipped_installer_failed(smoke: ModuleType) -> None:
    """0, 1, 2, 4, 4 is what the 0.7.0 installer scores on this sequence, and
    each of its three defects shows on a different step: the unexpanded Check
    at step 3, the missing collapse at step 4, the remove-first uninstall at
    step 5. All three have to be named, or a fix for one hides the other two."""
    problems = smoke._matrix_problems([0, 1, 2, 4, 4])
    assert len(problems) == 3
    assert "the second identical add-to-PATH upgrade: 2 owned" in problems[0]
    assert "an upgrade over a PATH an earlier build duplicated: 4 owned" in problems[1]
    assert "the uninstall, over a PATH an earlier build duplicated: 4 owned" in problems[2]


@pytest.mark.parametrize(
    ("observed", "step"),
    [
        # Each single defect, left in on its own, with the step it lands on.
        ([0, 1, 2, 2, 0], "the second identical add-to-PATH upgrade"),
        ([0, 1, 1, 2, 0], "an upgrade over a PATH an earlier build duplicated"),
        ([0, 1, 1, 1, 1], "the uninstall, over a PATH an earlier build duplicated"),
    ],
)
def test_each_defect_alone_still_fails_its_own_step(
    smoke: ModuleType, observed: list[int], step: str
) -> None:
    """The three guards in anastomosis.iss are independent, so the matrix has to
    catch each one on its own — a sequence that only measured the end state
    would pass an installer that still cannot repair a machine it broke. The
    FIRST problem reported is the step the defect lands on: an unexpanded Check
    also spoils every step after it, and a reader chasing a cause needs the
    earliest one first."""
    problems = smoke._matrix_problems(observed)
    assert problems
    assert step in problems[0]


def test_a_short_measurement_is_a_failure_not_a_pass(smoke: ModuleType) -> None:
    """A cycle that gave up after three steps must not read as three passes."""
    assert smoke._matrix_problems([0, 1, 1]) == ["expected 5 PATH measurements, got 3"]


def test_the_matrix_requires_one_segment_after_every_upgrade(smoke: ModuleType) -> None:
    """The numbers themselves are the contract from #281: an upgrade that adds
    a second segment, an upgrade that leaves a duplicate uncollapsed, or an
    uninstall that leaves anything, is the whole defect."""
    assert [expected for _label, expected in smoke._PATH_MATRIX] == [0, 1, 1, 1, 0]
    assert "uninstall" in smoke._PATH_MATRIX[-1][0]


def test_the_upgrades_select_the_add_to_path_task(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The matrix only measures anything if the task is actually chosen, and
    ``/MERGETASKS`` is the only way to choose it without a wizard — including
    the '!' spelling that deselects it for the task-off install. Each run also
    gets its own log, so the first upgrade's evidence is still there after the
    second one runs."""
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], *, timeout: int) -> Any:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(smoke, "_run", fake_run)
    monkeypatch.setattr(smoke, "_temp_dir", lambda: tmp_path)
    installer = tmp_path / "Anastomosis-Setup-0.0.0.exe"

    smoke.install(installer, merge_tasks="!addtopath", log_name="off.log")
    smoke.install(installer, merge_tasks="addtopath", log_name="upgrade-1.log")
    smoke.install(installer, merge_tasks="addtopath", log_name="upgrade-2.log")
    smoke.install(installer)

    assert "/MERGETASKS=!addtopath" in seen[0]
    assert "/MERGETASKS=addtopath" in seen[1]
    assert "/MERGETASKS=addtopath" in seen[2]
    assert not [arg for arg in seen[3] if arg.startswith("/MERGETASKS")]
    logs = [arg for cmd in seen for arg in cmd if arg.startswith("/LOG=")]
    assert len(set(logs)) == len(logs)


class _FakeInstaller:
    """An installer that edits a machine PATH the way anastomosis.iss does.

    A MODEL of the .iss, deliberately not the .iss itself — Pascal needs a
    Windows runner, and the real script is proved by the smoke job. What this
    proves is the other half of the same gate: that the matrix and its two
    injections actually reject an installer behaving the way the shipped one
    did. Each flag switches off exactly one of the three guards the fix added,
    so a matrix that stopped covering one of them fails here.
    """

    def __init__(
        self,
        path: str,
        *,
        expand: bool = True,
        collapse: bool = True,
        remove_all: bool = True,
        shared_decision: bool = True,
    ) -> None:
        self.path = path
        self.marker = False
        self.expand = expand
        self.collapse = collapse
        self.remove_all = remove_all
        self.shared_decision = shared_decision
        self.actions: list[str] = []

    def read(self) -> str:
        return self.path

    def write(self, value: str) -> None:
        self.path = value

    def _is_ours(self, segment: str) -> bool:
        return segment.upper() == _DIR.upper()

    def install(self, _installer: Path, *, merge_tasks: str | None = None, **_: object) -> None:
        self.actions.append(f"install({merge_tasks})")
        if self.collapse and self.marker:  # CurStepChanged(ssInstall)
            kept: list[str] = []
            seen = 0
            for segment in self.path.split(";"):
                if self._is_ours(segment):
                    seen += 1
                    if seen > 1:
                        continue
                kept.append(segment)
            self.path = ";".join(kept)
        if merge_tasks != "addtopath":
            return
        # [Registry] Check: NeedsAddPath. Without the expansion the literal
        # '{app}\cli' is compared, which matches no PATH on earth.
        present = self.expand and any(self._is_ours(s) for s in self.path.split(";"))
        if not present:
            self.path = f"{self.path};{_DIR}"
            # The marker is a SECOND [Registry] entry carrying the same Check,
            # evaluated after the append above has already landed.
            # shared_decision=False models asking the PATH again there instead
            # of reusing the one answer: it reads "already present", the marker
            # is never written, and both repair paths lose the gate they need.
            self.marker = self.shared_decision or not any(
                self._is_ours(s) for s in self.path.split(";")
            )

    def uninstall(self, **_: object) -> None:
        self.actions.append("uninstall")
        if self.marker:  # CurUninstallStepChanged(usUninstall)
            kept = []
            removed = False
            for segment in self.path.split(";"):
                if self._is_ours(segment) and (self.remove_all or not removed):
                    removed = True
                    continue
                kept.append(segment)
            self.path = ";".join(kept)
        self.marker = False


#: What the runner's machine PATH looks like before the cycle touches it.
_START_PATH = r"C:\Windows;;C:\Windows\System32;"
#: The two user-owned lookalikes the cycle seeds and must hand back untouched.
_SEEDS = [_DIR + "2", _DIR + r"\bin"]


@pytest.fixture
def machine(smoke: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Callable[..., _FakeInstaller]:
    """Point the PATH cycle at a fake registry and a fake installer."""

    def _make(**flags: bool) -> _FakeInstaller:
        fake = _FakeInstaller(_START_PATH, **flags)
        monkeypatch.setattr(smoke, "_machine_path", fake.read)
        monkeypatch.setattr(smoke, "_write_machine_path", fake.write)
        # The marker read is part of the same registry seam, and the cycle asks
        # for it after every step: off Windows there is no winreg to answer.
        monkeypatch.setattr(smoke, "_marker_state", lambda: f"64-bit view: {int(fake.marker)}")
        monkeypatch.setattr(smoke, "install", fake.install)
        monkeypatch.setattr(smoke, "uninstall", fake.uninstall)
        monkeypatch.setattr(smoke, "_cli_dir", lambda: _DIR)
        monkeypatch.setattr(smoke, "_user_path_segments", lambda: list(_SEEDS))
        return fake

    return _make


def test_the_cycle_passes_against_an_installer_that_behaves(
    smoke: ModuleType, machine: Callable[..., _FakeInstaller], capsys: Any
) -> None:
    """Five steps in the order #281 requires, and the machine PATH handed back
    exactly as it was found — seeds removed, nothing else moved.

    And every step reports the ownership marker beside its count, on the
    passing steps too. That is the whole story of the cycle in one column:
    nothing owned, then claimed by the upgrade, then given back — and when a
    step goes red, the reader can see which one lost it. The first red run
    reported counts alone, and the marker turned out to be the answer.
    """
    fake = machine()
    smoke.check_path_matrix(Path("Anastomosis-Setup-0.0.0.exe"))
    assert fake.actions == [
        "install(!addtopath)",
        "install(addtopath)",
        "install(addtopath)",
        "install(addtopath)",
        "uninstall",
    ]
    assert fake.path == _START_PATH

    out = capsys.readouterr().out
    markers = [line.strip() for line in out.splitlines() if line.strip().startswith("marker ")]
    assert len(markers) == len(smoke._PATH_MATRIX)
    assert markers[0] == "marker 64-bit view: 0"
    assert markers[1] == "marker 64-bit view: 1"
    assert markers[-1] == "marker 64-bit view: 0"


@pytest.mark.parametrize(
    ("guard", "step"),
    [
        ("expand", "the second identical add-to-PATH upgrade"),
        ("collapse", "an upgrade over a PATH an earlier build duplicated"),
        ("remove_all", "the uninstall, over a PATH an earlier build duplicated"),
        ("shared_decision", "an upgrade over a PATH an earlier build duplicated"),
    ],
)
def test_the_cycle_rejects_each_of_the_shipped_defects(
    smoke: ModuleType, machine: Callable[..., _FakeInstaller], guard: str, step: str
) -> None:
    """One guard removed at a time, because the two repair steps only exist to
    catch the two defects a clean install can no longer produce: drop either
    injection and this installer passes a gate written for it."""
    machine(**{guard: False})
    with pytest.raises(smoke.SmokeFailure) as excinfo:
        smoke.check_path_matrix(Path("Anastomosis-Setup-0.0.0.exe"))
    assert str(excinfo.value).startswith(f"after {step}")


def test_the_machine_path_is_restored_even_when_a_step_fails(
    smoke: ModuleType, machine: Callable[..., _FakeInstaller], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cycle seeds the PATH of the runner it is running on. Leaving the
    seeds there after a failure would hand every later step — and every later
    job on a reused runner — a machine PATH this test invented."""
    fake = machine()

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise smoke.SmokeFailure("the installer exited with code 1")

    monkeypatch.setattr(smoke, "install", refuse)
    with pytest.raises(smoke.SmokeFailure):
        smoke.check_path_matrix(Path("Anastomosis-Setup-0.0.0.exe"))
    assert fake.path == _START_PATH


def test_a_restore_that_cannot_run_does_not_replace_the_real_failure(
    smoke: ModuleType, machine: Callable[..., _FakeInstaller], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restore runs while an earlier failure is unwinding. A registry write
    that fails there — the runner's PATH held by another process, a permission
    the job turned out not to have — would otherwise surface as the reason the
    smoke failed, and the actual reason would never be printed at all."""
    fake = machine()
    writes = 0

    def refuse_second_write(value: str) -> None:
        nonlocal writes
        writes += 1
        if writes > 1:
            raise PermissionError("Access is denied")
        fake.write(value)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise smoke.SmokeFailure("the installer exited with code 1")

    monkeypatch.setattr(smoke, "_write_machine_path", refuse_second_write)
    monkeypatch.setattr(smoke, "install", refuse)
    with pytest.raises(smoke.SmokeFailure) as excinfo:
        smoke.check_path_matrix(Path("Anastomosis-Setup-0.0.0.exe"))
    assert "the installer exited with code 1" in str(excinfo.value)


def test_the_cycle_reproduces_the_runner_failure_this_step_was_written_for(
    smoke: ModuleType, machine: Callable[..., _FakeInstaller]
) -> None:
    """0, 1, 1, 2, 3 — what the real runner measured when the two [Registry]
    entries each asked the PATH for themselves. The append worked perfectly,
    which is why the first three steps passed and only the two marker-gated
    ones went red: an installer can add a segment and still be unable to prove
    it owns it."""
    machine(shared_decision=False)
    with pytest.raises(smoke.SmokeFailure) as excinfo:
        smoke.check_path_matrix(Path("Anastomosis-Setup-0.0.0.exe"))
    message = str(excinfo.value)
    assert message.startswith("after an upgrade over a PATH an earlier build duplicated: 2 owned")
    assert "the uninstall, over a PATH an earlier build duplicated: 3 owned" in message


def test_the_uninstall_writes_a_log_of_its_own(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The uninstaller is where the PATH segment is stripped, so when the count
    afterwards is wrong its log is the only account of what it decided — and
    the PATH cycle's uninstall needs a name of its own, or the first cycle's
    uninstall log is what a reader ends up staring at."""
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], *, timeout: int) -> Any:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(smoke, "_run", fake_run)
    monkeypatch.setattr(smoke, "_temp_dir", lambda: tmp_path)
    monkeypatch.setattr(smoke, "_uninstaller", lambda: tmp_path / "unins000.exe")
    monkeypatch.setattr(smoke, "_has_payload", lambda _app: False)
    monkeypatch.setattr(smoke, "_start_menu_group", lambda: tmp_path / "absent")

    smoke.uninstall()
    smoke.uninstall(log_name=smoke._PATH_LOG_UNINSTALL)

    logs = [arg for cmd in seen for arg in cmd if arg.startswith("/LOG=")]
    assert len(logs) == 2
    assert len(set(logs)) == 2
    assert logs[1].endswith(smoke._PATH_LOG_UNINSTALL)


def test_a_log_tail_is_bounded_and_says_so_when_there_is_none(
    smoke: ModuleType, tmp_path: Path, capsys: Any
) -> None:
    """An unbounded transcript buries the decisions it is printed for, and a
    silently skipped absent file hides the finding that the log was never
    written at all."""
    log = tmp_path / "install.log"
    log.write_text("\n".join(f"line {index}" for index in range(60)), encoding="utf-8")
    smoke._dump_tail("an installer log", log, lines=40)
    smoke._dump_tail("a log nobody wrote", tmp_path / "missing.log")

    out = capsys.readouterr().out
    assert "line 59" in out
    assert "line 20" in out
    assert "line 19" not in out
    assert "(absent)" in out


def test_a_diagnostic_that_cannot_run_does_not_replace_the_failure(
    smoke: ModuleType,
    machine: Callable[..., _FakeInstaller],
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    """The dump runs while a real failure is unwinding, and it reads a registry
    and a temp directory on a machine where something has already gone wrong.
    An error of its own there would replace the count that failed with a
    traceback about the log it was trying to print."""
    machine(shared_decision=False)

    def refuse() -> Path:
        raise RuntimeError("the temp directory is gone")

    monkeypatch.setattr(smoke, "_temp_dir", refuse)
    with pytest.raises(smoke.SmokeFailure) as excinfo:
        smoke.check_path_matrix(Path("Anastomosis-Setup-0.0.0.exe"))

    assert "an upgrade over a PATH an earlier build duplicated" in str(excinfo.value)
    assert "the PATH diagnostics could not be read (RuntimeError)" in capsys.readouterr().out


def test_a_failing_cycle_carries_its_own_diagnosis(
    smoke: ModuleType,
    machine: Callable[..., _FakeInstaller],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: Any,
) -> None:
    """The whole reason this exists. The first red run reported five numbers
    and nothing that explained them, and the answer — whether the marker the
    two failing steps gate on was ever written — was one registry read away."""
    machine(shared_decision=False)
    monkeypatch.setattr(smoke, "_temp_dir", lambda: tmp_path)
    monkeypatch.setattr(smoke, "_marker_state", lambda: "64-bit view: absent; 32-bit view: absent")
    (tmp_path / smoke._PATH_LOG_REPAIR).write_text(
        "\n".join(f"line {index}" for index in range(60)), encoding="utf-8"
    )

    with pytest.raises(smoke.SmokeFailure):
        smoke.check_path_matrix(Path("Anastomosis-Setup-0.0.0.exe"))

    out = capsys.readouterr().out
    assert "ownership marker Software\\Anastomosis\\PathAdded -> 64-bit view: absent" in out
    assert "line 59" in out
    assert "line 19" not in out
    assert "the uninstaller log" in out
