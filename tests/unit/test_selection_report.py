"""What the source's own selection rules left out is visible in the output.

`tests/fixtures/pf_tebra_v9/patient-encounters.tsv` holds 8 encounter rows. Six
render; the other two are excluded by `_skip_reason` — an empty SOAP note and an
adult growth chart. Both rules are sound. What was not was the reporting: the
run said `6 rendered, 0 skipped, 0 failed`, and "skipped" in that line means
"the file was already on disk", so on a first run it reads as "nothing was left
out". A plain `pipeline run` wrote no artifact recording the exclusions at all —
they survived only inside a C-CDA loss narrative and a JSON blob nested in a
FHIR Patient resource, neither of which `pipeline run` produces.

An operator reconciling 8 source rows against 6 charts had nothing in the output
to explain the gap, and the obvious conclusion — that the tool lost them — was
wrong but reasonable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import anastomosis.reconstruct.chromium as chromium
import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
from anastomosis.pipeline import SELECTION_REPORT_NAME, run_pipeline

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"

#: The two the fixture's rules exclude, and why.
EXPECTED = {
    "feedface-e000-0000-0000-000000000007": "empty_soap",
    "feedface-e000-0000-0000-000000000008": "adult_growth_chart",
}


class _FakeChromium:
    """A real PDF without a browser — the unit lane has none."""

    def __init__(self, **kwargs: object) -> None:
        pass

    def render(self, html: str, pdf_path: Path) -> None:
        import pymupdf

        from anastomosis.core.textutil import html_to_text

        doc = pymupdf.open()
        page = doc.new_page(width=612, height=792)
        page.insert_textbox(
            pymupdf.Rect(18, 18, 594, 774), html_to_text(html) or "(empty)", fontsize=7
        )
        doc.save(str(pdf_path))
        doc.close()

    def close(self) -> None:
        pass


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pymupdf", reason="the fake renderer writes a real PDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)


def _run(out: Path, events: list[Any] | None = None) -> Any:
    return run_pipeline(
        export_dir=FIXTURE,
        out=out,
        source="pf-tebra",
        pack="generic_soap",
        pack_dirs=None,
        force=False,
        section=None,
        qa=False,
        on_event=events.append if events is not None else None,
    )


def _report(out: Path) -> dict[str, Any]:
    return json.loads((out / SELECTION_REPORT_NAME).read_text(encoding="utf-8"))


def test_the_run_reports_how_many_encounters_the_rules_excluded(
    rendered: None, tmp_path: Path
) -> None:
    """The count is its own number, not folded into "skipped"."""
    events: list[Any] = []
    _run(tmp_path / "out", events)

    reconstruct = next(e for e in events if e.counts.get("rendered") is not None)
    assert reconstruct.counts["rendered"] == 6
    assert reconstruct.counts["excluded"] == len(EXPECTED)
    # Distinct from the idempotent skip, which means something else entirely.
    assert reconstruct.counts["skipped"] == 0


def test_the_report_names_every_excluded_encounter_and_why(rendered: None, tmp_path: Path) -> None:
    out = tmp_path / "out"
    _run(out)

    excluded = _report(out)["excluded"]
    assert {e["encounter_id"]: e["reason"] for e in excluded} == EXPECTED
    assert {e["rule_source"] for e in excluded} == {"pf_tebra"}


def test_the_report_is_written_even_when_nothing_was_excluded(
    rendered: None, tmp_path: Path
) -> None:
    """ "Nothing was left out" is the answer an operator most often needs, and an
    absent file cannot tell it apart from a run that never looked."""
    out = tmp_path / "out"
    _run(out)
    # The C-CDA fixture's rules exclude nothing, so it is the empty case.
    import anastomosis.sources.ccda  # noqa: F401 — registers the adapter

    other = tmp_path / "ccda_out"
    run_pipeline(
        export_dir=FIXTURE.parents[0] / "ccda",
        out=other,
        source="ccda",
        pack="generic_soap",
        pack_dirs=None,
        force=False,
        section=None,
        qa=False,
    )
    assert _report(other) == {"version": 1, "excluded": []}


def test_the_report_carries_no_patient_values(rendered: None, tmp_path: Path) -> None:
    """It sits beside the charts and gets read casually, so it holds source
    identifiers and rule names only — never a word of the chart it describes."""
    out = tmp_path / "out"
    _run(out)
    blob = (out / SELECTION_REPORT_NAME).read_text(encoding="utf-8")

    from anastomosis.sources import get_source

    for record in get_source("pf-tebra").load(FIXTURE):
        for value in (record.patient.family_name, record.patient.given_name):
            if value:
                assert value not in blob, f"a patient name reached the report: {value!r}"
        for entry in record.extensions.get("pf_tebra:skipped_encounters", []):
            encounter = entry["encounter"]
            for section in encounter.get("sections") or []:
                if text := (section.get("text") or "").strip():
                    assert text not in blob, "note text reached the report"
            if cc := (encounter.get("chief_complaint") or "").strip():
                assert cc not in blob, "a chief complaint reached the report"
