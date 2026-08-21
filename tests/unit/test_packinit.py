"""Unit tests for the shared pack-init command core (core/packinit.py).

Drives :func:`anastomosis.core.packinit.run_pack_init` directly — the analyze →
confirm → emit flow both the CLI and the GUI run. Synthetic 'sample' PDFs are
built with PyMuPDF (the test_packgen_emit / test_gui_controller pattern), so the
whole flow is exercised without a browser. All values are synthetic
(example-style names, never-issued dates); no patient-derived data appears.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf", reason="packinit tests need the render extra (PyMuPDF)")

from anastomosis.core.packinit import (  # noqa: E402
    PackInitCommand,
    run_pack_init,
)

# Distinct synthetic patients (each value unique → never recurs → never static).
_PATIENTS = [
    ("Synthia Example", "03/14/1985", "Hypertension follow-up"),
    ("Maxwell Sample", "07/04/1952", "Diabetes review"),
    ("Cleo Placeholder", "12/01/2021", "Well child visit"),
    ("Dale Specimen", "09/09/1970", "Annual physical"),
]


def _packgen_samples(tmp_path: Path, n: int = 4) -> Path:
    """A directory of distinct-patient synthetic sample PDFs (needs PyMuPDF)."""
    samples = tmp_path / "samples"
    samples.mkdir()
    for i in range(n):
        name, dob, complaint = _PATIENTS[i % len(_PATIENTS)]
        doc = pymupdf.open()
        page = doc.new_page(width=612, height=792)
        page.draw_rect(pymupdf.Rect(60, 95, 560, 110), fill=(0.9451, 0.9451, 0.9451), color=None)
        page.insert_text((60, 90), "SUBJECTIVE", fontsize=13, fontname="hebo")
        page.insert_text((60, 130), "OBJECTIVE", fontsize=13, fontname="hebo")
        page.insert_text((60, 200), "DOB:", fontsize=11, fontname="helv")
        page.insert_text((200, 200), dob, fontsize=11, fontname="helv")
        page.insert_text((60, 260), f"Patient {name} seen today.", fontsize=11, fontname="helv")
        page.insert_text((60, 280), complaint, fontsize=11, fontname="helv")
        page.insert_text((60, 760), "Confidential Example Clinic", fontsize=9, fontname="helv")
        doc.save(str(samples / f"sample{i}.pdf"))
        doc.close()
    return samples


def test_invalid_pack_name() -> None:
    result = run_pack_init(PackInitCommand(samples=["/anywhere"], name="Bad-Name", confirmed=True))
    assert result.ok is False
    assert result.error == "InvalidPackName"
    assert result.summary == []
    assert result.pack_dir is None
    assert result.draft_md is None


def test_never_raises_on_malformed_command() -> None:
    """The "never raises into the caller" contract holds even when a caller
    ignores the type hints (a non-str name, a non-list samples)."""
    bad_name = run_pack_init(PackInitCommand(samples=["/x"], name="Bad-Name"))
    assert bad_name.error == "InvalidPackName"
    non_str = run_pack_init(PackInitCommand(samples=["/x"], name=123))  # type: ignore[arg-type]
    assert non_str.error == "InvalidPackName"
    no_samples = run_pack_init(PackInitCommand(samples=None, name="ok_name"))  # type: ignore[arg-type]
    assert no_samples.error == "NoSamplesFound"


def test_no_samples_found(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run_pack_init(PackInitCommand(samples=[str(empty)], name="acme_soap", confirmed=True))
    assert result.ok is False
    assert result.error == "NoSamplesFound"
    assert result.sample_count == 0


def test_confirmation_required_returns_summary_and_writes_nothing(tmp_path: Path) -> None:
    samples = _packgen_samples(tmp_path)
    out = tmp_path / "packs"
    result = run_pack_init(
        PackInitCommand(samples=[str(samples)], name="acme_soap", out_dir=out, confirmed=False)
    )
    assert result.ok is False
    assert result.error == "ConfirmationRequired"
    assert result.summary, "the refusal must carry the PHI-safe summary to confirm"
    assert result.caveat
    assert result.sample_count == 4
    assert result.pack_dir is None
    assert result.draft_md is None
    # Nothing was written.
    assert not (out / "acme_soap").exists()


def test_confirmation_required_is_phi_safe(tmp_path: Path) -> None:
    """The refusal summary carries static template text only — no patient value."""
    samples = _packgen_samples(tmp_path)
    result = run_pack_init(
        PackInitCommand(samples=[str(samples)], name="acme_soap", confirmed=False)
    )
    blob = " ".join(result.summary)
    for name, dob, complaint in _PATIENTS:
        assert name.split()[0] not in blob
        assert dob not in blob
        assert complaint not in blob


def test_happy_emits_loadable_draft(tmp_path: Path) -> None:
    samples = _packgen_samples(tmp_path)
    out = tmp_path / "packs"
    result = run_pack_init(
        PackInitCommand(
            samples=[str(samples)],
            name="acme_soap",
            display="Acme SOAP",
            out_dir=out,
            confirmed=True,
        )
    )
    assert result.ok is True
    assert result.error is None
    assert result.pack_dir is not None
    assert result.pack_dir.is_dir()
    assert (result.pack_dir / "pack.yaml").is_file()
    assert (result.pack_dir / "DRAFT.md").is_file()
    assert result.draft_md and "DRAFT" in result.draft_md
    assert result.summary
    assert result.sample_count == 4


def test_low_confidence_single_sample(tmp_path: Path) -> None:
    samples = _packgen_samples(tmp_path, n=1)
    result = run_pack_init(
        PackInitCommand(samples=[str(samples)], name="acme_soap", confirmed=False)
    )
    assert result.error == "ConfirmationRequired"
    assert result.low_confidence is True
    assert result.sample_count == 1
