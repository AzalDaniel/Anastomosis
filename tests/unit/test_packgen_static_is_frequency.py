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

This file holds the reproduction and pins the wording that now tells the truth
about it. It does NOT assert the threshold, which is the maintainer's call —
raising it to a true intersection changes what every learned pack contains.

PHI: the fixture below is synthetic by construction — invented names, a
textbook diagnosis, and a standard OMB ethnicity category. Nothing copied.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anastomosis.packgen.emit import SAME_PATIENT_CAVEAT

#: Three different patients. Two share a diagnosis and an ethnicity, which is
#: an ordinary thing for two patients to do.
_PATIENTS = (
    ("Alder Quill", "Type 2 diabetes mellitus", "Not Hispanic or Latino"),
    ("Brannoc Vane", "Type 2 diabetes mellitus", "Not Hispanic or Latino"),
    ("Cressida Yew", "Seasonal allergic rhinitis", "Hispanic or Latino"),
)


def _samples(tmp_path: Path) -> list[Path]:
    pymupdf = pytest.importorskip("pymupdf", reason="the learner reads PDFs")
    paths = []
    for index, (name, diagnosis, ethnicity) in enumerate(_PATIENTS):
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
            "Signed electronically",  # real chrome
        ):
            page.insert_text((72, y), line, fontsize=11)
            y += 18
        path = tmp_path / f"sample{index}.pdf"
        doc.save(str(path))
        doc.close()
        paths.append(path)
    return paths


def test_a_value_two_patients_share_classifies_as_template_text(tmp_path: Path) -> None:
    """The reproduction, kept live so the caveat below cannot become untrue.

    If a later change makes the split content-aware — a label shape, a stable
    page position, anything but frequency — this test fails, and the words it
    guards need revisiting in the same breath.
    """
    from anastomosis.packgen import analyze, extract_samples

    analysis = analyze(extract_samples(_samples(tmp_path)))
    static = set(analysis.static_text)

    # The genuine chrome is there, which is the part that works.
    assert {"VISIT NOTE", "Patient:", "Assessment:"} <= static, sorted(static)

    # And so are two values belonging to patients, because two of three
    # patients happened to share them.
    assert "Type 2 diabetes mellitus" in static, sorted(static)
    assert "Not Hispanic or Latino" in static, sorted(static)

    # A value in exactly ONE sample is still excluded — the narrower guarantee
    # frequency does support, and the only one now claimed.
    for name, _diagnosis, _ethnicity in _PATIENTS:
        assert name not in static, f"{name!r} reached the static set"


def test_the_caveat_names_the_failure_that_actually_happens() -> None:
    """It warned about one patient's chart handed in three times, and stopped there.

    That failure is real and the wording for it stays. What was missing is the
    one #200 reproduced: distinct patients, which the prompt asks the operator
    to confirm, and which the confirmation therefore implied was the safe case.
    """
    caveat = SAME_PATIENT_CAVEAT

    assert "MUST be from DIFFERENT patients" in caveat
    assert "NOT on their own enough" in caveat
    # Named, so the operator knows what to look for in the list.
    for kind in ("diagnosis", "ethnicity", "referring provider", "clinic address"):
        assert kind in caveat, f"the caveat does not mention a shared {kind}"
    # And what to do about it.
    assert "delete anything that belongs to a patient" in caveat


def test_no_surface_promises_the_list_is_free_of_patient_data() -> None:
    """Three places said so; the list cannot keep that promise."""
    from anastomosis.gui.shell import _WEB_DIR

    repo = Path(__file__).resolve().parents[2]
    claims = {
        repo / "src" / "anastomosis" / "packgen" / "emit.py": "never recur and never reach here",
        repo / "src" / "anastomosis" / "packgen" / "infer.py": "never per-patient",
        repo
        / "src"
        / "anastomosis"
        / "cli_commands"
        / "packsrc.py": "Inferred design[/bold] (PHI-safe summary)",
    }
    for path, claim in claims.items():
        assert claim not in path.read_text(encoding="utf-8"), (
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
