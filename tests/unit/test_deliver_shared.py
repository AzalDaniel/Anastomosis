"""Tests for the mechanics the file-writing deliverers share.

Two properties, both of them "a chart must never go missing or land in the
wrong slot":

* :func:`~anastomosis.deliver._shared.budgeted_copy_name` — the DESTINATION
  name for a copied chart, cut to fit the path budget of the tree it is being
  copied into. Renderer chart names run to ~617 characters
  (``{family}_{given}_{dos}_{type}.pdf``, each component capped at
  ``MAX_NAME_CHARS``); copying one of those into a delivered tree used to fail
  with an OSError the deliverers logged and continued past, which is a chart
  silently absent from what the operator hands over.
* :func:`~anastomosis.deliver._shared.claim_delivered_name` — the per-run
  ledger. The deliverers write with ``mkdir(exist_ok=True)`` /
  ``write_bytes`` / ``write_text``, so two source ids resolving to one
  delivered name MERGE rather than fail. A second, different claimant raises.

Plus :func:`~anastomosis.deliver._shared.copy_claimed_chart`, which chains
the two together with the actual copy — the one budget→claim→copy sequence
every deliverer's chart-copy site now shares.

Synthetic ids only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import anastomosis.deliver._shared as _shared
from anastomosis.core.textutil import HASH_TAG_CHARS, MAX_PATH_CHARS
from anastomosis.deliver._shared import (
    DeliveredNameCollision,
    budgeted_copy_name,
    claim_delivered_name,
    copy_claimed_chart,
)

# The renderer's own shape, at the length safe_name permits per component.
_RENDERER_NAME = "Fixture_Ada_05-10-2023_" + "S" * 200 + ".pdf"


def test_copy_name_passes_a_normal_chart_through_unchanged(tmp_path: Path) -> None:
    # PIN: every real chart name is delivered byte-identical. Budgeting must be
    # invisible until a path actually cannot hold the name.
    name = "Fixture_Ada_05-10-2023_SOAP.pdf"
    assert budgeted_copy_name(tmp_path, name) == name


def test_copy_name_cuts_a_renderer_length_name_to_fit(tmp_path: Path) -> None:
    target = tmp_path / "patients" / "feedface-0000-0000-0000-0000000000aa" / "pdfs"
    target.mkdir(parents=True)

    delivered = budgeted_copy_name(target, _RENDERER_NAME)

    assert delivered.endswith(".pdf")
    assert len(str(target / delivered)) <= MAX_PATH_CHARS
    # ...and the cut name is a name the filesystem actually accepts.
    (target / delivered).write_bytes(b"%PDF-1.7 fake\n")
    assert (target / delivered).is_file()


def test_copy_name_keeps_two_long_charts_distinct(tmp_path: Path) -> None:
    # Two charts differing only past the cut must not collapse onto one file —
    # that is one encounter's chart overwriting another's.
    target = tmp_path / "pdfs"
    target.mkdir()
    stem = "Fixture_Ada_05-10-2023_" + "S" * 200
    first = budgeted_copy_name(target, f"{stem}_one.pdf")
    second = budgeted_copy_name(target, f"{stem}_two.pdf")
    assert first != second


def test_copy_name_raises_when_no_distinct_name_fits(tmp_path: Path) -> None:
    # The loud failure: a destination directory so deep that not even a hash
    # tag fits. Refusing beats copying onto a name that could collide.
    deep = tmp_path / ("d" * (MAX_PATH_CHARS - len(str(tmp_path)) - HASH_TAG_CHARS))
    with pytest.raises(ValueError, match="path budget"):
        budgeted_copy_name(deep, _RENDERER_NAME)


def test_claim_records_a_name_once(tmp_path: Path) -> None:
    claims: dict[str, str] = {}
    claim_delivered_name(claims, "MRN_1234", "MRN 1234", kind="patient directory")
    assert claims == {"MRN_1234": "MRN 1234"}


def test_claim_is_idempotent_for_the_same_source_id() -> None:
    # A record legitimately re-claiming its own slot in one run keeps it.
    claims: dict[str, str] = {}
    claim_delivered_name(claims, "MRN_1234", "MRN 1234", kind="patient directory")
    claim_delivered_name(claims, "MRN_1234", "MRN 1234", kind="patient directory")
    assert claims == {"MRN_1234": "MRN 1234"}


def test_claim_refuses_a_second_different_source_id() -> None:
    """The sanitize-collapse collision: ``MRN 1234`` and ``MRN/1234`` both
    become ``MRN_1234``. Silently, that files one patient's record on top of
    another's; loudly, it stops the run."""
    claims: dict[str, str] = {}
    claim_delivered_name(claims, "MRN_1234", "MRN 1234", kind="patient directory")
    with pytest.raises(DeliveredNameCollision, match="patient directory"):
        claim_delivered_name(claims, "MRN_1234", "MRN/1234", kind="patient directory")


def test_copy_claimed_chart_delivers_and_claims_on_success(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7 fake\n")
    target_dir = tmp_path / "pdfs"
    target_dir.mkdir()
    claims: dict[str, str] = {}

    delivered, failure = copy_claimed_chart(target_dir, claims, source, "source.pdf")

    assert failure is None
    assert delivered == "source.pdf"
    assert (target_dir / "source.pdf").read_bytes() == b"%PDF-1.7 fake\n"
    assert claims == {"source.pdf": "source.pdf"}


def test_copy_claimed_chart_still_claims_the_name_on_a_failed_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy failure returns ``(None, failure)`` — no file lands — but the
    name is claimed regardless, exactly as the pre-extraction call sites did
    (budget+claim always ran before the copy, never conditioned on it)."""
    monkeypatch.setattr(_shared, "copy_delivered_file", lambda source, destination: "OSError")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7 fake\n")
    target_dir = tmp_path / "pdfs"
    target_dir.mkdir()
    claims: dict[str, str] = {}

    delivered, failure = copy_claimed_chart(target_dir, claims, source, "source.pdf")

    assert delivered is None
    assert failure == "OSError"
    assert not (target_dir / "source.pdf").exists()
    assert claims == {"source.pdf": "source.pdf"}


def test_claim_message_carries_no_source_value() -> None:
    # PHI: ids reach the message only as run-scoped safe_log_id surrogates, and
    # the delivered NAME (built from a source id) never appears at all.
    claims: dict[str, str] = {}
    claim_delivered_name(claims, "MRN_1234", "MRN 1234", kind="patient directory")
    with pytest.raises(DeliveredNameCollision) as excinfo:
        claim_delivered_name(claims, "MRN_1234", "MRN/1234", kind="patient directory")
    message = str(excinfo.value)
    assert "MRN 1234" not in message
    assert "MRN/1234" not in message
    assert "MRN_1234" not in message
    assert message.count("id:") == 2
