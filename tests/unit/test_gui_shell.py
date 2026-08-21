"""GUI shell tests — the process-level Windows preparation, without a window.

:mod:`anastomosis.gui.shell` is the thin pywebview adapter, so almost all of it
needs a real window and is out of reach here. Its module-level *preparation*
helpers are not: they only read process state and return a value, so the
Windows-only branch is exercised on any platform by monkeypatching
``sys.platform`` — the same guard style :mod:`anastomosis.core.locking` uses for
its platform arms.

Two properties are pinned:

* :func:`~anastomosis.gui.shell._webview2_user_data_folder` returns the
  per-user profile folder ``webview.start(storage_path=...)`` is given — the
  ONLY route to WebView2's UserDataFolder in a pywebview app, since pywebview
  assigns that property itself and the ``WEBVIEW2_USER_DATA_FOLDER``
  environment variable is never consulted. It must create what it names,
  respect an operator's override, return ``None`` off Windows (so ``launch``
  omits the argument and pywebview's own default stands), and never raise — a
  warn-only helper that could throw would take the whole GUI down with it.
* :func:`~anastomosis.gui.shell._apply_remote_debugging_port` routes the
  diagnostics-only ``ANAST_GUI_REMOTE_DEBUGGING_PORT`` into pywebview's
  ``REMOTE_DEBUGGING_PORT`` setting (WebView2 ignores
  ``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`` when the host app sets
  AdditionalBrowserArguments, which pywebview always does). Unset must be a
  no-op — an open CDP port on a PHI app is a disclosure surface, so it is
  opt-in — and a bad value must never raise.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import pytest

from anastomosis.gui.shell import (
    _REMOTE_DEBUG_PORT_ENV,
    _REMOTE_DEBUG_SETTING,
    _WEBVIEW2_USER_DATA_ENV,
    _apply_remote_debugging_port,
    _webview2_user_data_folder,
)

_SHELL_LOGGER = "anastomosis.gui.shell"


@pytest.fixture
def local_appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An empty %LOCALAPPDATA% under tmp_path, with the override variable unset."""
    monkeypatch.delenv(_WEBVIEW2_USER_DATA_ENV, raising=False)
    local = tmp_path / "LocalAppData"
    local.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return local


def test_returns_a_created_per_user_folder_on_windows(
    monkeypatch: pytest.MonkeyPatch, local_appdata: Path
) -> None:
    """On Windows: the app-named folder under %LOCALAPPDATA%, already created."""
    monkeypatch.setattr(sys, "platform", "win32")

    folder = _webview2_user_data_folder()

    expected = local_appdata / "Anastomosis" / "WebView2"
    assert folder == str(expected)
    # The folder must EXIST: pywebview is handed a path, not a promise.
    assert expected.is_dir()


def test_creates_the_whole_missing_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A %LOCALAPPDATA% that does not exist yet is created, parents and all."""
    monkeypatch.delenv(_WEBVIEW2_USER_DATA_ENV, raising=False)
    absent = tmp_path / "never" / "created" / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(absent))
    monkeypatch.setattr(sys, "platform", "win32")

    folder = _webview2_user_data_folder()

    expected = absent / "Anastomosis" / "WebView2"
    assert folder == str(expected)
    assert expected.is_dir()


def test_is_idempotent_across_calls(monkeypatch: pytest.MonkeyPatch, local_appdata: Path) -> None:
    """Calling twice returns the same folder (exist_ok, same value)."""
    monkeypatch.setattr(sys, "platform", "win32")

    first = _webview2_user_data_folder()
    second = _webview2_user_data_folder()

    assert first is not None and first == second
    assert (local_appdata / "Anastomosis" / "WebView2").is_dir()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_no_op_off_windows(
    monkeypatch: pytest.MonkeyPatch, local_appdata: Path, platform: str
) -> None:
    """Off Windows there is no WebView2: say nothing, create nothing.

    ``None`` is what makes ``launch`` omit the ``storage_path`` argument, so
    the non-Windows backends keep exactly the behaviour they had.
    """
    monkeypatch.setattr(sys, "platform", platform)

    assert _webview2_user_data_folder() is None
    assert not (local_appdata / "Anastomosis").exists()


def test_respects_an_operator_supplied_folder(
    monkeypatch: pytest.MonkeyPatch, local_appdata: Path, tmp_path: Path
) -> None:
    """A value in the environment is a deliberate override — pass it through."""
    chosen = tmp_path / "chosen-profile"
    monkeypatch.setenv(_WEBVIEW2_USER_DATA_ENV, str(chosen))
    monkeypatch.setattr(sys, "platform", "win32")

    folder = _webview2_user_data_folder()

    assert folder == str(chosen)
    assert chosen.is_dir()
    # ...and nothing was created under the default location.
    assert not (local_appdata / "Anastomosis").exists()


def test_warns_and_never_raises_without_localappdata(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No %LOCALAPPDATA%: warn with the exception TYPE, return None, start anyway."""
    monkeypatch.delenv(_WEBVIEW2_USER_DATA_ENV, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")

    with caplog.at_level(logging.WARNING, logger=_SHELL_LOGGER):
        assert _webview2_user_data_folder() is None  # must not raise

    messages = [record.getMessage() for record in caplog.records]
    assert any("webview2 user-data folder not set" in message for message in messages)
    # exc_tag discipline: the TYPE name, never the exception's own text.
    assert any("KeyError" in message for message in messages)


# --- the diagnostics-only remote debugging port ------------------------------


def _settings() -> dict[str, Any]:
    """A stand-in for ``webview.settings`` carrying the real default (None)."""
    return {_REMOTE_DEBUG_SETTING: None}


def test_remote_debugging_port_is_applied_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid port lands in pywebview's setting as an int."""
    monkeypatch.setenv(_REMOTE_DEBUG_PORT_ENV, "9222")
    settings = _settings()

    _apply_remote_debugging_port(settings)

    assert settings[_REMOTE_DEBUG_SETTING] == 9222


def test_remote_debugging_port_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset: the setting is left alone. An open CDP port is opt-in, never default."""
    monkeypatch.delenv(_REMOTE_DEBUG_PORT_ENV, raising=False)
    settings = _settings()

    _apply_remote_debugging_port(settings)

    assert settings[_REMOTE_DEBUG_SETTING] is None


@pytest.mark.parametrize("value", ["", "not-a-port", "0", "65536", "-1", "9222 ", "9222.0"])
def test_remote_debugging_port_ignores_a_bad_value(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A malformed or out-of-range value is ignored, never raised, never applied."""
    monkeypatch.setenv(_REMOTE_DEBUG_PORT_ENV, value)
    settings = _settings()

    _apply_remote_debugging_port(settings)  # must not raise

    assert settings[_REMOTE_DEBUG_SETTING] is None


def test_remote_debugging_port_survives_a_pywebview_without_the_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """pywebview's settings mapping refuses unknown keys — warn, never crash.

    ``webview.settings`` is an ImmutableDict: assigning a key it does not carry
    raises KeyError. A future pywebview that renamed the setting must degrade to
    a GUI without a debugging port, not a GUI that will not start.
    """
    monkeypatch.setenv(_REMOTE_DEBUG_PORT_ENV, "9222")

    class _Refusing(dict[str, Any]):
        def __setitem__(self, key: str, value: Any) -> None:
            raise KeyError(key)

    settings = _Refusing()
    with caplog.at_level(logging.WARNING, logger=_SHELL_LOGGER):
        _apply_remote_debugging_port(settings)  # must not raise

    assert not settings
    messages = [record.getMessage() for record in caplog.records]
    assert any("remote debugging port not set" in message for message in messages)
    assert any("KeyError" in message for message in messages)
