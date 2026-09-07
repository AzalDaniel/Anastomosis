"""The Windows smoke test's coming-alive waits, tested off Windows.
The step needs a Windows runner with WebView2, but its FAILURE MESSAGE
does not (#270): a release gate once went red with "Timeout 30000ms
exceeded" and nothing else, giving a reader no way to tell a hung
bridge from a slow one.

The waiting and reporting are separated from the browser driving, so
what an operator reads is exercised against stubs; whether the real
WebView2 comes alive is exercised only by the Windows job.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SMOKE = Path(__file__).resolve().parents[2] / "packaging" / "smoke_windows.py"


@pytest.fixture(scope="module")
def smoke() -> ModuleType:
    """Load the smoke script by path — it is a packaging script, not a module
    of the installed package, and importing it must not require Windows."""
    spec = importlib.util.spec_from_file_location("smoke_windows", _SMOKE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StubPage:
    """A page that answers a fixed number of waits and then times out."""

    def __init__(self, *, succeed: int, state: str | Exception = "bridge=live") -> None:
        self._left = succeed
        self._state = state
        self.waited: list[str] = []

    def wait_for_function(self, predicate: str, *, timeout: int) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        self.waited.append(predicate)
        if self._left <= 0:
            raise PlaywrightTimeout(f"Timeout {timeout}ms exceeded")
        self._left -= 1

    def evaluate(self, _script: str) -> Any:
        if isinstance(self._state, Exception):
            raise self._state
        return self._state


pytest.importorskip("playwright", reason="the stub raises playwright's TimeoutError")


def test_both_signals_are_awaited_in_order(smoke: ModuleType, capsys: Any) -> None:
    page = _StubPage(succeed=2)
    smoke._await_liveness(page)

    assert len(page.waited) == 2
    assert "dataset.bridge" in page.waited[0]
    assert "about-version" in page.waited[1]
    # And each says how much of its budget it used, so a shrinking margin is
    # visible in the log while it is still passing.
    out = capsys.readouterr().out
    assert "the bridge going live:" in out
    assert "the first info() round-trip:" in out
    assert "ms of" in out


def test_a_timeout_names_the_signal_that_never_arrived(smoke: ModuleType) -> None:
    """The whole point. "Timeout 30000ms exceeded" told a reader the budget ran
    out and nothing else."""
    page = _StubPage(succeed=0)
    with pytest.raises(smoke.SmokeFailure) as excinfo:
        smoke._await_liveness(page)
    message = str(excinfo.value)
    assert "the bridge going live never happened" in message
    assert "90s" in message  # the budget it actually had
    assert "bridge=live" in message  # and the state the page was in


def test_a_bridge_that_lives_but_never_answers_is_a_different_failure(
    smoke: ModuleType,
) -> None:
    """A hung info() round-trip and a hung bridge are distinct failure messages."""
    page = _StubPage(succeed=1)
    with pytest.raises(smoke.SmokeFailure) as excinfo:
        smoke._await_liveness(page)
    assert "the first info() round-trip never happened" in str(excinfo.value)


def test_a_page_that_cannot_be_read_is_itself_the_diagnosis(smoke: ModuleType) -> None:
    """A diagnostic must never replace the real failure with an error of its
    own — the same rule the GUI dump already follows."""
    page = _StubPage(succeed=0, state=RuntimeError("target closed"))
    with pytest.raises(smoke.SmokeFailure) as excinfo:
        smoke._await_liveness(page)
    message = str(excinfo.value)
    assert "the bridge going live never happened" in message
    assert "the page could not be read (RuntimeError)" in message


def test_the_budgets_are_sized_for_a_cold_runner(smoke: ModuleType) -> None:
    """The measured failure took ~43s longer than a passing control before
    giving up on a 30s budget, so none of the three may be back under it."""
    for budget in (smoke._PAGE_TIMEOUT_MS, smoke._BRIDGE_TIMEOUT_MS, smoke._INFO_TIMEOUT_MS):
        assert budget >= 30_000
    assert smoke._BRIDGE_TIMEOUT_MS > 30_000, "the cold-start wait is the one that ran out"


def test_every_liveness_step_has_a_name_a_predicate_and_a_budget(smoke: ModuleType) -> None:
    """A step added without one of the three would reintroduce the anonymous
    timeout this replaced."""
    assert smoke._LIVENESS_STEPS
    for label, predicate, budget in smoke._LIVENESS_STEPS:
        assert label and predicate.startswith("() =>") and budget > 0
