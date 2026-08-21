"""GUI shell tests — the process-level Windows preparation, without a window.

:mod:`anastomosis.gui.shell` is the thin pywebview adapter, so almost all of it
needs a real window and is out of reach here. Its module-level *preparation*
helpers are not: they only read and write process state, so the Windows-only
branch is exercised on any platform by monkeypatching ``sys.platform`` — the
same guard style :mod:`anastomosis.core.locking` uses for its platform arms.

The property under test is
:func:`~anastomosis.gui.shell._ensure_webview2_user_data_folder`: an app
installed under Program Files must hand WebView2 a WRITABLE profile folder
(``%LOCALAPPDATA%\\Anastomosis\\WebView2``) instead of letting it default to a
directory beside the exe, which a standard user cannot write. It must respect an
operator's own value, do nothing off Windows, and never raise — a warn-only
helper that could throw would take the whole GUI down with it.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from anastomosis.gui.shell import _WEBVIEW2_USER_DATA_ENV, _ensure_webview2_user_data_folder

_SHELL_LOGGER = "anastomosis.gui.shell"


@pytest.fixture
def local_appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An empty %LOCALAPPDATA% under tmp_path, with the target variable unset.

    Deleting the variable through monkeypatch is what guarantees cleanup: the
    helper SETS it, and monkeypatch only restores what it was told about.
    """
    monkeypatch.delenv(_WEBVIEW2_USER_DATA_ENV, raising=False)
    local = tmp_path / "LocalAppData"
    local.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return local


def test_sets_a_writable_user_data_folder_on_windows(
    monkeypatch: pytest.MonkeyPatch, local_appdata: Path
) -> None:
    """On Windows with the variable unset: point WebView2 under %LOCALAPPDATA%."""
    monkeypatch.setattr(sys, "platform", "win32")

    _ensure_webview2_user_data_folder()

    expected = local_appdata / "Anastomosis" / "WebView2"
    assert os.environ[_WEBVIEW2_USER_DATA_ENV] == str(expected)
    # The folder must EXIST: WebView2 is handed a path, not a promise.
    assert expected.is_dir()


def test_creates_the_whole_missing_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A %LOCALAPPDATA% that does not exist yet is created, parents and all."""
    monkeypatch.delenv(_WEBVIEW2_USER_DATA_ENV, raising=False)
    absent = tmp_path / "never" / "created" / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(absent))
    monkeypatch.setattr(sys, "platform", "win32")

    _ensure_webview2_user_data_folder()

    expected = absent / "Anastomosis" / "WebView2"
    assert expected.is_dir()
    assert os.environ[_WEBVIEW2_USER_DATA_ENV] == str(expected)


def test_is_idempotent_across_calls(monkeypatch: pytest.MonkeyPatch, local_appdata: Path) -> None:
    """Calling twice is a no-op the second time (exist_ok, and the value stands)."""
    monkeypatch.setattr(sys, "platform", "win32")

    _ensure_webview2_user_data_folder()
    first = os.environ[_WEBVIEW2_USER_DATA_ENV]
    _ensure_webview2_user_data_folder()

    assert os.environ[_WEBVIEW2_USER_DATA_ENV] == first
    assert (local_appdata / "Anastomosis" / "WebView2").is_dir()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_no_op_off_windows(
    monkeypatch: pytest.MonkeyPatch, local_appdata: Path, platform: str
) -> None:
    """Off Windows there is no WebView2 runtime: leave the environment untouched."""
    monkeypatch.setattr(sys, "platform", platform)

    _ensure_webview2_user_data_folder()

    assert _WEBVIEW2_USER_DATA_ENV not in os.environ
    assert not (local_appdata / "Anastomosis").exists()


def test_respects_an_operator_supplied_folder(
    monkeypatch: pytest.MonkeyPatch, local_appdata: Path, tmp_path: Path
) -> None:
    """A value already in the environment is a deliberate override — keep it."""
    chosen = str(tmp_path / "chosen-profile")
    monkeypatch.setenv(_WEBVIEW2_USER_DATA_ENV, chosen)
    monkeypatch.setattr(sys, "platform", "win32")

    _ensure_webview2_user_data_folder()

    assert os.environ[_WEBVIEW2_USER_DATA_ENV] == chosen
    # ...and nothing was created under the default location.
    assert not (local_appdata / "Anastomosis").exists()


def test_warns_and_never_raises_without_localappdata(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No %LOCALAPPDATA%: warn with the exception TYPE and let the GUI start."""
    monkeypatch.delenv(_WEBVIEW2_USER_DATA_ENV, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")

    with caplog.at_level(logging.WARNING, logger=_SHELL_LOGGER):
        _ensure_webview2_user_data_folder()  # must not raise

    assert _WEBVIEW2_USER_DATA_ENV not in os.environ
    messages = [record.getMessage() for record in caplog.records]
    assert any("webview2 user-data folder not set" in message for message in messages)
    # exc_tag discipline: the TYPE name, never the exception's own text.
    assert any("KeyError" in message for message in messages)
