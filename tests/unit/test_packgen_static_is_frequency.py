"""What "static" actually means, in the words the operator reads.

``pack init --from-samples`` classifies text as static (form) or
per-patient (values) by two rules together: a string must appear on EVERY
sample, AND occupy a page position no other sample's value ever occupies.
Frequency alone lets a shared value recur through (#200); position alone
lets a value every patient shares in a fixed cell through, quarantined and
captioned, never claimed clean. Fixture is synthetic: invented names, a
textbook diagnosis, a standard OMB category.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anastomosis.packgen.emit import SAME_PATIENT_CAVEAT

#: The shared diagnosis this issue reproduced (#200): two of the three
#: patients carry it, an ordinary thing for two patients to do.
_SHARED_DX = "Type 2 diabetes mellitus"

#: The same diagnosis again, on EVERY patient, in a problem list whose rows
#: shift with how many diagnoses came before. Frequency cannot touch this one:
#: it is on every sample. What gives it away is the page — each row it lands in
#: holds somebody else's diagnosis in another chart, so it never owns a slot.
_LISTED_DX = "Essential hypertension"

#: What ALL THREE share, in a FIXED cell. This is the residual: no sample ever
#: prints anything else in that slot, so nothing distinguishes it from a label
#: the form printed, and it still reaches the pack. Quarantined and captioned
#: rather than claimed to be safe — the tests below hold that line.
_SHARED_PAYER = "Cascadia Health"

#: name, assessment (two of three share it), ethnicity (ditto), and a problem
#: list that puts _LISTED_DX at a different row in each chart.
_PATIENTS = (
    ("Alder Quill", _SHARED_DX, "Not Hispanic or Latino", (_LISTED_DX, "Vitamin D deficiency")),
    ("Brannoc Vane", _SHARED_DX, "Not Hispanic or Latino", ("Chronic sinusitis", _LISTED_DX)),
    (
        "Cressida Yew",
        "Seasonal allergic rhinitis",
        "Hispanic or Latino",
        ("Iron deficiency anemia", "Migraine without aura", _LISTED_DX),
    ),
)


def _samples(tmp_path: Path) -> list[Path]:
    pymupdf = pytest.importorskip("pymupdf", reason="the learner reads PDFs")
    paths = []
    for index, (name, diagnosis, ethnicity, problems) in enumerate(_PATIENTS):
        doc = pymupdf.open()
        page = doc.new_page()
        y = 72.0
        for line in (
            "VISIT NOTE",  # real chrome
            "Patient:",
            name,  # a per-patient value, in one sample
            "Assessment:",
            diagnosis,  # a per-patient value, in two
            "Ethnicity:",
            ethnicity,  # ditto
            "Payer:",
            _SHARED_PAYER,  # a value every sample shares, in a fixed cell
            "Problem list:",
            *problems,  # a shared diagnosis at a row that moves
            "Signed electronically",  # real chrome
        ):
            page.insert_text((72, y), line, fontsize=11)
            y += 18
        path = tmp_path / f"sample{index}.pdf"
        doc.save(str(path))
        doc.close()
        paths.append(path)
    return paths


def test_a_value_two_patients_share_no_longer_classifies_as_template_text(
    tmp_path: Path,
) -> None:
    """The assessment and ethnicity are on two of three charts, so the
    every-sample rule stops them; "Signed electronically" is on all three
    at the same place, so it is kept — the filter must not take real
    template chrome with it."""
    from anastomosis.packgen import analyze, extract_samples

    analysis = analyze(extract_samples(_samples(tmp_path)))
    static = set(analysis.static_text)

    assert {"VISIT NOTE", "Patient:", "Assessment:", "Signed electronically"} <= static, sorted(
        static
    )

    assert _SHARED_DX not in static, sorted(static)
    assert "Not Hispanic or Latino" not in static, sorted(static)

    # And a value in exactly one sample, as before.
    for name, *_rest in _PATIENTS:
        assert name not in static, f"{name!r} reached the static set"


def test_a_shared_diagnosis_on_every_chart_is_caught_by_the_page_not_the_count(
    tmp_path: Path,
) -> None:
    """On all three charts, so frequency says nothing about it; what settles
    it is the page — the problem list puts it at a different row each time,
    and every row it occupies holds somebody else's diagnosis elsewhere, so
    it never owns a place."""
    from anastomosis.packgen import analyze, extract_samples

    analysis = analyze(extract_samples(_samples(tmp_path)))
    static = set(analysis.static_text)

    # The premise: it really is on every sample, so the count cannot exclude it.
    assert all(_LISTED_DX in problems for *_head, problems in _PATIENTS)
    assert _LISTED_DX not in static, sorted(static)
    # While the label above the list, which does hold still, is kept.
    assert "Problem list:" in static, sorted(static)


def test_a_value_only_one_chart_carries_is_excluded_however_alone_it_stands(
    tmp_path: Path,
) -> None:
    """A line only ONE chart has, at a spot no other chart reaches, owns
    its slot by default — nobody else got that far down the page to
    compete for it. The every-sample rule is what catches this, not the
    page test."""
    pymupdf = pytest.importorskip("pymupdf", reason="the learner reads PDFs")
    from anastomosis.packgen import analyze, extract_samples

    alone = "Penicillin anaphylaxis"
    paths = []
    for index, extra in enumerate([(), (alone,)]):
        doc = pymupdf.open()
        page = doc.new_page()
        y = 72.0
        for line in ("VISIT NOTE", "Allergies:", *extra):
            page.insert_text((72, y), line, fontsize=11)
            y += 18
        path = tmp_path / f"one{index}.pdf"
        doc.save(str(path))
        doc.close()
        paths.append(path)

    static = set(analyze(extract_samples(paths)).static_text)
    assert "Allergies:" in static, sorted(static)
    assert alone not in static, sorted(static)


def test_a_value_every_patient_shares_still_gets_through(tmp_path: Path) -> None:
    """Nothing on the page distinguishes a payer all three patients share,
    printed in the same cell every time, from a label the form printed
    there — no filter reading only the samples can. It reaches the pack,
    quarantined and captioned."""
    from anastomosis.packgen import analyze, extract_samples

    analysis = analyze(extract_samples(_samples(tmp_path)))
    assert _SHARED_PAYER in set(analysis.static_text)


def test_the_caveat_names_the_failure_that_actually_happens() -> None:
    """The caveat still warns about one patient's chart handed in three
    times, but must also name what actually still gets through post-#200:
    a value EVERY sample shares in a fixed cell — not a shared diagnosis or
    ethnicity, which the every-sample-and-position rule now catches."""
    caveat = SAME_PATIENT_CAVEAT

    assert "MUST be from DIFFERENT patients" in caveat
    assert "NOT on their own enough" in caveat
    # Named, so the operator knows what to look for in the list.
    for kind in ("referring provider", "clinic address", "phone number"):
        assert kind in caveat, f"the caveat does not mention a shared {kind}"
    # And that recurring is not the proof it reads as.
    assert "not a proof" in caveat
    # And what to do about it.
    assert "delete anything that belongs to a patient" in caveat


def test_no_surface_promises_the_list_is_free_of_patient_data() -> None:
    """Every place that said so; the list cannot keep that promise."""
    from anastomosis.gui.shell import _WEB_DIR

    # A source sweep can only forbid a phrasing the file does not also quote
    # back (e.g. inside "is what this said"). The entries below are the
    # wordings no file states OR quotes; the emitted files, which carry no
    # such commentary, are swept properly two tests down.
    repo = Path(__file__).resolve().parents[2]
    packgen = repo / "src" / "anastomosis" / "packgen"
    claims = {
        packgen / "emit.py": (
            "never recur and never reach here",
            # The two that survived the #200 sweep because emit.py writes them
            # INTO the pack rather than saying them about it.
            "(template labels/",
            "boilerplate, not patient data",
        ),
        packgen / "infer.py": (
            "never per-patient",
            'A candidate is "static" (template, not per-patient)',
        ),
        repo / "src" / "anastomosis" / "cli_commands" / "packsrc.py": (
            "Inferred design[/bold] (PHI-safe summary)",
            "produce the PHI-safe summary",
        ),
    }
    for path, forbidden in claims.items():
        body = path.read_text(encoding="utf-8")
        for claim in forbidden:
            assert claim not in body, (
                f"{path.name} still claims {claim!r}, which the frequency split cannot support"
            )

    # The GUI said it in the plainest words of the three, and said it twice —
    # once over the learned LABELS, once over the learned column MATCH-UP. Only
    # the first was untrue: the match-up shows column names, never values. So
    # this counts rather than forbids, and would fail if the wrong one were the
    # survivor.
    markup = (_WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert markup.count(">No patient data is shown below.<") == 1, (
        "the note over the learned labels is the one a frequency count cannot keep"
    )
    # And what the layout panel says instead — asserted positively, because a
    # "this string is absent" check trips on the comment explaining why it is
    # absent, which is how this test failed twice while the markup was right.
    assert "value two patients happen to share repeats too" in markup


def _flat(text: str) -> str:
    """Whitespace-insensitive compare — the note is line-wrapped per surface."""
    return " ".join(text.split())


def test_sample_text_lands_in_the_quarantine_and_in_no_other_file(tmp_path: Path) -> None:
    """Sample-derived text must live in UNPLACED.txt only: template.html
    and DRAFT.md both travel (copied into every pack derived from this
    one), so a diagnosis leaking into either — even as an HTML comment —
    ships everywhere the pack goes. One file to delete, not two to scrub."""
    from anastomosis.packgen import analyze, extract_samples
    from anastomosis.packgen.emit import STATIC_LIST_NOTE, UNPLACED_NAME, emit_draft_pack

    analysis = analyze(extract_samples(_samples(tmp_path)))
    pack_dir = emit_draft_pack(
        analysis, name="acme_soap", display="ACME Clinic", out_dir=tmp_path / "out"
    )

    # The premise: this fixture really does produce something worth quarantining.
    # All three of its patients share this payer, in the same cell, so nothing
    # on the page tells it apart from a label (see the residual test above).
    quarantine = (pack_dir / UNPLACED_NAME).read_text(encoding="utf-8")
    assert _SHARED_PAYER in quarantine, "the fixture lost the reproduction"

    # And it is in NOTHING else the generator wrote.
    for path in sorted(pack_dir.rglob("*")):
        if not path.is_file() or path.name == UNPLACED_NAME:
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        assert _SHARED_PAYER not in body, f"{path.name} carries a string taken from the samples"

    # The quarantine still says what recurrence does not prove — the operator
    # decides what to keep while looking at the list, not four sections above it.
    assert _flat(STATIC_LIST_NOTE) in _flat(quarantine)
    for claim in ("not patient data", "boilerplate", "template labels"):
        assert claim not in quarantine, f"the quarantine calls the list {claim!r}"

    # And the two travelling files still point at it, so nothing is silently lost.
    for name in ("template.html", "DRAFT.md"):
        assert UNPLACED_NAME in (pack_dir / name).read_text(encoding="utf-8"), (
            f"{name} drops the strings without saying where they went"
        )


def test_a_single_sample_draft_does_not_claim_nothing_was_dropped(tmp_path: Path) -> None:
    """With ``low_confidence`` the emitter writes no sample-derived text at
    all, so DRAFT.md must not read the empty result as "all static text
    mapped to known header fields" and repeat the losslessness claim under
    it."""
    from anastomosis.packgen import analyze, extract_samples
    from anastomosis.packgen.emit import emit_draft_pack

    analysis = analyze(extract_samples(_samples(tmp_path)[:1]))
    assert analysis.low_confidence is True
    assert analysis.static_text, "the fixture must give the learner something to withhold"

    pack_dir = emit_draft_pack(
        analysis, name="acme_soap", display="ACME Clinic", out_dir=tmp_path / "out"
    )
    draft = (pack_dir / "DRAFT.md").read_text(encoding="utf-8")

    assert "nothing is dropped" not in draft
    assert "all static text mapped to known header fields" not in draft
    assert "deliberate withholding" in draft
    # And the withholding held: no sample-derived text in either file.
    template = (pack_dir / "template.html").read_text(encoding="utf-8")
    for name, *_rest in _PATIENTS[:1]:
        assert name not in draft and name not in template
