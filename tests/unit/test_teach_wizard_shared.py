"""The two teach wizards run one choreography, not two copies of it.

Both wizards — learn a template pack from sample PDFs, learn a new source format
from one example export — stop halfway on purpose. They analyze, then refuse
with `ConfirmationRequired` so a person can confirm what was found before
anything is written, and the JS fetches the stashed result on the terminal event
to render that summary.

Which terminal event carries the checkpoint is the part that matters. It has to
be `done`: send it as `error` and the wizard's ordinary middle step lands on the
failure branch, so the operator never sees the summary they are meant to
approve. Nothing crashes — the run just stops with a red banner where a
confirmation prompt belonged.

That rule used to be written out in both consoles. The 6-line-window duplication
scan could not see it, because the two copies differ only in which stage name
and which stash attribute they name, so one could have drifted with every test
still green. The guard below reads syntax rather than behaviour for the same
reason the C-CDA mirror guard does.
"""

from __future__ import annotations

import ast
from pathlib import Path

from anastomosis.gui.consoles.packgen import PackgenConsole
from anastomosis.gui.consoles.source import SourceConsole
from anastomosis.gui.consoles.wizard import WizardConsole
from anastomosis.gui.jobs import GuiJobRunner

_SRC = Path(__file__).resolve().parents[2] / "src" / "anastomosis"
_WIZARDS = {
    "packgen": _SRC / "gui" / "consoles" / "packgen.py",
    "source": _SRC / "gui" / "consoles" / "source.py",
}


def _calls(path: Path) -> set[str]:
    """Every `a.b.c(...)` attribute call in the module, as a dotted name."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        parts, target = [], node.func
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
            out.add(".".join(reversed(parts)))
    return out


def test_both_teach_wizards_run_the_shared_choreography() -> None:
    assert issubclass(PackgenConsole, WizardConsole)
    assert issubclass(SourceConsole, WizardConsole)


def test_neither_wizard_reaches_the_job_runner_on_its_own() -> None:
    """The base is the only route to the runner, so there is one copy to drift.

    A console that calls `self._jobs.submit` itself has re-grown its own worker,
    its own terminal-event routing and its own stash — which is exactly the pair
    of copies this replaced.
    """
    for name, path in _WIZARDS.items():
        assert "self._jobs.submit" not in _calls(path), (
            f"{name} submits its own job again — the choreography belongs to WizardConsole"
        )


def test_neither_wizard_decides_its_own_terminal_event() -> None:
    """`ConfirmationRequired`-is-`done` is stated once, in the base."""
    routing = 'if result.get("ok") or result.get("error") == "ConfirmationRequired"'
    base = (_SRC / "gui" / "consoles" / "wizard.py").read_text(encoding="utf-8")
    assert routing in base, "the routing rule moved; this guard needs updating with it"
    for name, path in _WIZARDS.items():
        body = path.read_text(encoding="utf-8")
        assert "stage_event(" not in body, (
            f"{name} emits its own stage event again — the terminal event is the base's call"
        )


def test_the_checkpoint_closes_the_run_as_done_not_as_failure() -> None:
    """Both wizards, driven through the shared base, agree on the rule.

    Exercised through `_submit_step` with a canned step rather than a real
    analyze: the point under test is the routing, and a real run would need
    sample PDFs to reach the same two lines.
    """
    for console_cls, stage in ((PackgenConsole, "packgen"), (SourceConsole, "source")):
        for result, expected in (
            ({"ok": False, "error": "ConfirmationRequired"}, "stage"),
            ({"ok": True}, "stage"),
            ({"ok": False, "error": "InvalidPackName"}, "error"),
        ):
            events: list[dict[str, object]] = []
            runner = GuiJobRunner(events.append)
            console = console_cls(events.append, runner)
            assert console._submit_step(lambda r=result: r) == {"ok": True, "started": True}
            runner.join(5)

            terminal = [e for e in events if e.get("stage") == stage][-1]
            assert terminal["type"] == expected, (result, terminal)
            if expected == "stage":
                assert terminal["state"] == "done"
            assert console._last_result()["ok"] is result["ok"]


def test_a_crash_in_the_step_is_stashed_as_a_type_name_only() -> None:
    """A worker crash never reaches the operator as a traceback or a value."""

    def _boom() -> dict[str, object]:
        raise ValueError("/synthetic/path/Doe_Jane.pdf")

    events: list[dict[str, object]] = []
    runner = GuiJobRunner(events.append)
    console = PackgenConsole(events.append, runner)
    console._submit_step(_boom)
    runner.join(5)

    errors = [e for e in events if e.get("type") == "error"]
    assert len(errors) == 1, "a crash must emit exactly one error, not a doubled pair"
    assert errors[0]["error"] == "ValueError"
    assert console._last_result() == {"ok": False, "error": "ValueError"}
    assert "Doe_Jane" not in repr(events), "the exception's message reached the operator"
