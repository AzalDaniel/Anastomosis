"""A machine that cannot render says so once, with the remedy.
`ChromiumRenderer.__init__` raises with the sentence naming what to
install. Catching the exception per encounter and keeping only its
TYPE would answer with six copies of

    render failed for encounter id:d47f4012d088 (RuntimeError)

and no line saying what to do — a property of the MACHINE, reported
once, not per encounter.
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
    """A renderer this machine cannot build: substitutes the class the
    engine actually constructs (the pattern the rest of this suite
    uses), rather than an import-denying meta_path hook — `playwright`
    is already in `sys.modules` by the time this file runs in a
    full-suite pass, so a finder would never see the deny."""

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
    """The remedy is OURS: Playwright's own message is not under our
    control, so only the exception TYPE goes through, never its prose.
    Pinned on the constructor's contract — it takes a type NAME, so no
    parameter can carry a library sentence."""
    message = str(RendererUnavailable("ModuleNotFoundError"))
    assert message.count("ModuleNotFoundError") == 1
    assert message.endswith(INSTALL_HINT)


def test_it_is_still_a_runtime_error() -> None:
    """Subclassed rather than replaced: anything that already caught the old
    RuntimeError keeps working."""
    assert issubclass(RendererUnavailable, RuntimeError)
