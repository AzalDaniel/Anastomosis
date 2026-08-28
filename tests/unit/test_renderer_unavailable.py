"""A machine that cannot render says so once, with the remedy.

`ChromiumRenderer.__init__` raises with the sentence that names what to
install. The engine caught every exception per encounter and kept only the
exception TYPE, so a base install answered:

    render failed for encounter id:d47f4012d088 (RuntimeError)   x6

six times, and threw away the one line that said what to do. The PHI rule is
right for an exception that may carry patient data; it should not erase the
renderer's own deterministic install hint. And the condition is a property of
the MACHINE, not of any one chart, so reporting it per encounter was wrong
whatever the message said.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import anastomosis.reconstruct.chromium as chromium
import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
from anastomosis.pipeline import PipelineError, run_pipeline
from anastomosis.reconstruct.chromium import INSTALL_HINT, RendererUnavailable

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"


class _NoRenderer:
    """A renderer this machine cannot build.

    NOT an import-denying meta_path hook, which was the first attempt here and
    is the wrong mechanism: `playwright` is already in `sys.modules` by the time
    this file runs in a full-suite pass, and a cached module never reaches a
    finder. The deny silently did nothing, these tests launched REAL Chromium
    instances and never closed them, and the browser was broken for every test
    after — `bundled Chromium: Error`, and 21 further tests skipped.

    Substituting the class the engine actually constructs is the pattern the
    rest of this suite uses, and it touches no global state.
    """

    def __init__(self, **kwargs: object) -> None:
        raise RendererUnavailable("ImportError")

    def render(self, html: str, pdf_path: Path) -> None: ...  # pragma: no cover

    def close(self) -> None: ...  # pragma: no cover


@pytest.fixture
def no_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chromium, "ChromiumRenderer", _NoRenderer)


def test_the_message_names_the_remedy() -> None:
    message = str(RendererUnavailable("ImportError"))
    assert INSTALL_HINT in message
    assert "ImportError" in message, "the exception type rides along for a support request"


def test_a_base_install_fails_once_not_once_per_chart(no_renderer: None, tmp_path: Path) -> None:
    """Six encounters, ONE failure, and the failure carries the remedy."""
    with pytest.raises(PipelineError) as caught:
        run_pipeline(
            export_dir=FIXTURE,
            out=tmp_path / "out",
            source="pf-tebra",
            pack="generic_soap",
            pack_dirs=None,
            force=False,
            section=None,
            qa=False,
        )

    error = caught.value
    # Exit 2 — "a capability this run needs is not available here", the code
    # this pipeline already uses for an unavailable pack. Not 1, which means a
    # chart failed to render; no chart was ever attempted.
    assert error.exit_code == 2
    assert INSTALL_HINT in str(error)
    # Not the old per-encounter shape: no list of failures, one statement.
    assert not error.failed
    assert "encounter" not in str(error).lower()


def test_the_message_carries_no_third_party_text() -> None:
    """The remedy is OURS. Playwright's own message is not under our control,
    and forwarding an uncontrolled string is how something unexpected reaches a
    console — so the exception TYPE goes through and the prose does not.

    Pinned on the constructor's contract: it takes a type NAME, so there is no
    parameter through which a library sentence could arrive.
    """
    message = str(RendererUnavailable("ModuleNotFoundError"))
    assert message.count("ModuleNotFoundError") == 1
    assert message.endswith(INSTALL_HINT)


def test_it_is_still_a_runtime_error() -> None:
    """Subclassed rather than replaced: anything that already caught the old
    RuntimeError keeps working."""
    assert issubclass(RendererUnavailable, RuntimeError)
