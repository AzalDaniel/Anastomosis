"""What "static" actually means, said in the words the operator reads.

``pack init --from-samples`` splits sample text into "static" (the form) and
per-patient (the values) by counting how many samples each string appears in.
Three of the places that describe that split called it a proof:

* ``emit.py``: *"Per-patient values never recur and never reach here."*
* ``infer.py``: *"Per-patient values, recurring in fewer samples, are
  excluded."*
* the GUI, directly above the list: *"No patient data is shown below."*

None of that is true. A frequency count cannot tell who wrote a string, and
with three samples the bar is two — so a diagnosis or an ethnicity that two of
three DIFFERENT patients share classifies as template chrome and is written
verbatim into ``template.html`` and ``DRAFT.md`` (#200).

That sweep read the source files and the GUI markup, and missed the copies
emit.py writes *into* the pack: the header of the UNPLACED comment in
``template.html`` and the "Unplaced static text" paragraph in ``DRAFT.md``,
both of which called the list "template labels/boilerplate, not patient data"
directly above the values two patients shared. Those are the words the
operator reads at the moment they decide what to keep, so they are the ones
that mattered most. The sweep below now covers the emitted files too.

This file holds that reproduction. The split is no longer a frequency count: a
string now has to be on EVERY sample AND own a place on the page nothing else
occupies, so the shared diagnosis and the shared ethnicity below no longer
reach the pack at all. What survives is the residual the filter cannot catch —
a value ALL of the patients share, in a fixed cell, with no competitor to give
it away — and the wording that tells the operator to look for exactly that.

PHI: the fixture below is synthetic by construction — invented names, a
textbook diagnosis, and a standard OMB ethnicity category. Nothing copied.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anastomosis.packgen.emit import SAME_PATIENT_CAVEAT

#: The shared diagnosis this issue reproduced. Two of the three patients carry
#: it, which is an ordinary thing for two patients to do — and used to be
#: enough to promote it to template chrome.
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
    """The reproduction, now run against the fix.

    The assessment and the ethnicity are on two of three charts, so the
    every-sample rule is what stops them. "Signed electronically" is on all
    three, at the same place every time, so it is kept — a filter that took the
    labels with it would be no use.
    """
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
    """The case frequency cannot reach, and the reason the rule has two halves.

    This diagnosis is on all three charts, so being on every sample says
    nothing about it. What settles it is where it lands: the problem list puts
    it at a different row in each chart, and every row it occupies holds
    somebody else's diagnosis in one of the others. It never owns a place on
    the page, so it is not the form's furniture.
    """
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
    """The half the count still has to do, and the reason the rule keeps it.

    Owning a slot means nothing else was ever printed there — and a line only
    ONE chart has, at a spot no other chart reaches, owns its slot by default.
    Nobody was competing for it because nobody else got that far down the page.
    So being on every sample is not redundant with the page test; it is what
    catches the value that is alone rather than fixed.
    """
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
    """The residual, stated rather than hoped away.

    Nothing on the page distinguishes a payer all three patients share, printed
    in the same cell every time, from a label the form printed there. No filter
    that reads only the samples can. So it still reaches the pack — which is
    why the list is quarantined and captioned, and why the caveat tells the
    operator to look for this exact shape.
    """
    from anastomosis.packgen import analyze, extract_samples

    analysis = analyze(extract_samples(_samples(tmp_path)))
    assert _SHARED_PAYER in set(analysis.static_text)


def test_the_caveat_names_the_failure_that_actually_happens() -> None:
    """It warned about one patient's chart handed in three times, and stopped there.

    That failure is real and the wording for it stays. What was missing was the
    one #200 reproduced: distinct patients, which the prompt asks the operator
    to confirm, and which the confirmation therefore implied was the safe case.

    The examples it names have moved with the fix, and had to. A shared
    diagnosis and a shared ethnicity are caught now, so telling an operator to
    hunt for those would send them looking for the wrong thing; what still gets
    through is a value EVERY sample shares in a fixed cell, so those are what
    the caveat names.
    """
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

    # A source sweep can only forbid a phrasing the file does not also quote,
    # and this codebase confesses in quotations — the docstrings that used to
    # make the claim now print it back with "is what this said" after it. So
    # the entries below are the wordings no file states OR quotes; the emitted
    # files, which carry no such commentary, are swept properly two tests down.
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
    """One file carries text taken from the samples, and deleting it is the job.

    This assertion is the inverse of the one it replaces, and deliberately so.
    That test pinned the shared diagnosis as PRESENT in template.html and
    DRAFT.md — it was documenting the leak while checking the wording printed
    above it. Wording was the right fix for the claim; it is not a fix for the
    string being in those files.

    Both of them travel. template.html renders every future patient's chart and
    is what gets copied when a second pack is derived from this one; DRAFT.md
    is the sheet handed over with it. A previous patient's diagnosis in either
    is carried everywhere the pack goes, and being an HTML comment is what
    makes it easy never to notice.

    So the strings sit in UNPLACED.txt, which renders nothing and is imported by
    nothing, and the operator's remedy stops depending on their diligence: they
    delete one file rather than reading two and remembering a third.
    """
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
    """One sample withholds every string, and the draft used to report the opposite.

    With ``low_confidence`` the emitter writes no sample-derived text at all —
    the right call. But DRAFT.md read the empty result as "all static text
    mapped to known header fields" and repeated the losslessness line under it,
    telling the operator the pack held everything the learner saw.
    """
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
