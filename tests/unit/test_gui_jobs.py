"""Direct tests for GuiJobRunner, the single owner of the async-job
choreography. The controller-level tests exercise the runner through its
public methods (busy guards, spawn failures, terminal events); these pin
the runner's own contract in isolation so a future consumer can rely on
it without reading the controller.
"""

from __future__ import annotations

import threading

from anastomosis.gui.jobs import GuiJob, GuiJobRunner


class _RecordingEmit:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self._lock = threading.Lock()

    def __call__(self, event: dict[str, object]) -> None:
        with self._lock:
            self.events.append(dict(event))


def _wait_released(runner: GuiJobRunner, timeout: float = 5.0) -> None:
    """Wait for the busy guard to release (the worker's finally ran)."""
    deadline = threading.Event()

    def _poll() -> None:
        while not runner.acquire():
            pass
        runner.release()
        deadline.set()

    t = threading.Thread(target=_poll, daemon=True)
    t.start()
    assert deadline.wait(timeout), "runner never released the busy guard"


def test_submit_runs_worker_and_releases() -> None:
    emit = _RecordingEmit()
    runner = GuiJobRunner(emit)
    ran = threading.Event()

    result = runner.submit(GuiJob(name="x", worker=ran.set))
    assert result == {"ok": True, "started": True}
    assert ran.wait(5.0)
    _wait_released(runner)


def test_submit_rejects_while_busy_then_recovers() -> None:
    emit = _RecordingEmit()
    runner = GuiJobRunner(emit)
    gate = threading.Event()
    started = threading.Event()

    def _blocking() -> None:
        started.set()
        gate.wait(5.0)

    first = runner.submit(GuiJob(name="one", worker=_blocking))
    assert first == {"ok": True, "started": True}
    assert started.wait(5.0)

    # Deterministic rejection: the guard was acquired synchronously.
    second = runner.submit(GuiJob(name="two", worker=lambda: None))
    assert second == {"ok": False, "error": "Busy"}

    gate.set()
    _wait_released(runner)
    # After release, a new submit succeeds — no wedge.
    third = runner.submit(GuiJob(name="three", worker=lambda: None))
    assert third == {"ok": True, "started": True}
    _wait_released(runner)


def test_worker_exception_becomes_error_event_never_raises() -> None:
    emit = _RecordingEmit()
    runner = GuiJobRunner(emit)
    escaped: list[str] = []

    def _boom() -> None:
        raise ValueError("secret patient value must not appear")

    previous = threading.excepthook
    threading.excepthook = lambda args: escaped.append(type(args.exc_value).__name__)
    try:
        assert runner.submit(GuiJob(name="j", worker=_boom)) == {"ok": True, "started": True}
        _wait_released(runner)
    finally:
        threading.excepthook = previous

    errors = [e for e in emit.events if e.get("type") == "error"]
    assert len(errors) == 1
    assert errors[0]["stage"] == "j"
    # PHI-safe: the exception TYPE name only, never the message.
    assert errors[0]["error"] == "ValueError"
    assert "secret" not in str(emit.events)
    # "never raises" is half the name of this test and was not being checked.
    # An ordinary Exception is reported and SWALLOWED — only a BaseException is
    # re-raised, and that difference is the whole shape of the two arms.
    assert escaped == [], escaped


def test_a_base_exception_still_reports_before_it_kills_the_thread() -> None:
    """A run that started must always end with something the operator can
    see (#117): the upload engine models process death as a BaseException
    on purpose (see FakeCrash), so it must be caught, reported, AND
    re-raised — telling the operator is not pretending it did not happen,
    so the thread still dies and the guard still releases."""
    emit = _RecordingEmit()
    runner = GuiJobRunner(emit)
    raised: list[str] = []

    class _Kill(KeyboardInterrupt):
        """Stands in for FakeCrash: a BaseException, not an Exception."""

    def _boom() -> None:
        raise _Kill

    def _hook(args: threading.ExceptHookArgs) -> None:
        raised.append(type(args.exc_value).__name__)

    previous = threading.excepthook
    threading.excepthook = _hook
    try:
        assert runner.submit(GuiJob(name="j", worker=_boom)) == {"ok": True, "started": True}
        _wait_released(runner)
    finally:
        threading.excepthook = previous

    errors = [e for e in emit.events if e.get("type") == "error"]
    assert len(errors) == 1, emit.events
    assert errors[0]["stage"] == "j"
    assert errors[0]["error"] == "_Kill"
    # Re-raised, not swallowed: the thread died the way it was going to.
    assert raised == ["_Kill"], raised


def test_stage_overrides_thread_name_for_error_channel() -> None:
    emit = _RecordingEmit()
    runner = GuiJobRunner(emit)

    def _boom() -> None:
        raise RuntimeError("x")

    runner.submit(GuiJob(name="pipeline", stage="run_pipeline", worker=_boom))
    _wait_released(runner)
    errors = [e for e in emit.events if e.get("type") == "error"]
    assert errors and errors[0]["stage"] == "run_pipeline"


def test_cleanup_runs_after_worker_and_on_spawn_failure(
    monkeypatch: object,
) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    emit = _RecordingEmit()
    runner = GuiJobRunner(emit)
    order: list[str] = []

    runner.submit(
        GuiJob(
            name="ok",
            worker=lambda: order.append("worker"),
            cleanup=lambda: order.append("cleanup"),
        )
    )
    _wait_released(runner)
    assert order == ["worker", "cleanup"]

    # Spawn failure: cleanup still runs, guard released, error dict returned.
    class _ExplodingThread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    cleaned = threading.Event()
    monkeypatch.setattr(threading, "Thread", _ExplodingThread)
    result = runner.submit(GuiJob(name="boom", worker=lambda: None, cleanup=cleaned.set))
    assert result == {"ok": False, "error": "RuntimeError"}
    assert cleaned.is_set()
    monkeypatch.undo()
    # Guard not wedged.
    assert runner.acquire() is True
    runner.release()


def test_on_start_failure_releases_and_cleans() -> None:
    emit = _RecordingEmit()
    runner = GuiJobRunner(emit)
    cleaned = threading.Event()

    def _bad_start() -> None:
        raise OSError("disk?")

    result = runner.submit(
        GuiJob(name="j", worker=lambda: None, on_start=_bad_start, cleanup=cleaned.set)
    )
    assert result == {"ok": False, "error": "OSError"}
    assert cleaned.is_set()
    assert runner.acquire() is True  # not wedged
    runner.release()


def test_cleanup_failure_never_masks_release() -> None:
    emit = _RecordingEmit()
    runner = GuiJobRunner(emit)

    def _bad_cleanup() -> None:
        raise RuntimeError("cleanup broke")

    runner.submit(GuiJob(name="j", worker=lambda: None, cleanup=_bad_cleanup))
    _wait_released(runner)  # release still happened despite the cleanup crash
