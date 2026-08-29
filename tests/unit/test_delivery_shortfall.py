"""What a delivery could not file has to reach the person who ran it (#225).

Both deliverers already knew, at the moment they discovered it, which charts
they could not put in front of a patient. The knowledge went into a log line
and no further: it was absent from the returned result, so the CLI summary and
the GUI rail could not have shown it even if they had wanted to.

Two things go wrong, and they are not the same thing:

* **missing** — this run's index names a chart and the file is not there. That
  is a chart nobody will see.
* **unattributed** — a chart is there and belongs to no patient in this run, so
  the archive files it under ``unattributed/`` rather than guessing. Not lost,
  but nobody opens a directory they were not told about.

Synthetic pf_tebra fixture data throughout; the assertions are on counts and on
wording, never on a chart filename (which carries a patient's name and their
date of service).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import anastomosis.sources.pf_tebra  # noqa: F401 — registers the source adapter
from anastomosis.core.model import PatientRecord
from anastomosis.deliver.archive import ArchiveDeliverer
from anastomosis.deliver.bundle import BundleDeliverer
from anastomosis.deliver.render_index import RenderEntry, RenderIndex
from anastomosis.sources import get_source

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"


@pytest.fixture
def records() -> list[PatientRecord]:
    return list(get_source("pf-tebra").load(FIXTURE))


def _charts(records: list[PatientRecord], charts_dir: Path) -> list[RenderEntry]:
    """One believable chart per encounter, plus the index the engine writes.

    The filename shape does not matter to any of these tests — attribution is
    by the index — so it is kept deliberately boring, and free of patient text.
    """
    charts_dir.mkdir(parents=True, exist_ok=True)
    entries = [
        RenderEntry(
            pdf=f"chart-{n:02d}.pdf", patient_id=record.patient.id, encounter_id=encounter.id
        )
        for n, (record, encounter) in enumerate((r, e) for r in records for e in r.encounters)
    ]
    for entry in entries:
        (charts_dir / entry.pdf).write_bytes(b"%PDF-1.7 fake")
    RenderIndex.from_entries(entries).write(charts_dir)
    return entries


def test_the_archive_counts_a_chart_the_index_names_and_that_is_not_there(
    records: list[PatientRecord], tmp_path: Path
) -> None:
    charts = tmp_path / "charts"
    entries = _charts(records, charts)
    (charts / entries[0].pdf).unlink()

    result = ArchiveDeliverer().deliver(records, charts, tmp_path / "archive")

    assert result.missing_count == 1
    assert result.pdf_count == len(entries) - 1
    assert result.unattributed_count == 0


def test_the_archive_counts_a_chart_no_patient_in_this_run_claimed(
    records: list[PatientRecord], tmp_path: Path
) -> None:
    """A leftover from an earlier run into the same folder.

    It is delivered — to ``unattributed/``, which is right — and the count is
    the only thing that will make anyone look in there.
    """
    charts = tmp_path / "charts"
    entries = _charts(records, charts)
    (charts / "chart-99.pdf").write_bytes(b"%PDF-1.7 fake")

    result = ArchiveDeliverer().deliver(records, charts, tmp_path / "archive")

    assert result.unattributed_count == 1
    assert result.missing_count == 0
    assert result.pdf_count == len(entries)
    assert len(list((tmp_path / "archive" / "unattributed").glob("*.pdf"))) == 1


def test_the_encounter_page_says_its_chart_is_missing(
    records: list[PatientRecord], tmp_path: Path
) -> None:
    """The page a clinician reads, not just the summary the operator reads.

    The chart link is emitted behind ``{% if pdf_name %}``, so a chart that did
    not arrive used to leave a page that looked complete — the word "chart"
    never appeared on it at all.
    """
    charts = tmp_path / "charts"
    entries = _charts(records, charts)
    (charts / entries[0].pdf).unlink()

    ArchiveDeliverer().deliver(records, charts, tmp_path / "archive")

    pages = sorted((tmp_path / "archive").rglob("encounters/*.html"))
    said = [p for p in pages if "missing-chart" in p.read_text(encoding="utf-8")]
    assert len(said) == 1, "exactly the one encounter whose chart vanished"
    assert entries[0].encounter_id in said[0].name

    text = re.sub(r"<[^>]+>", " ", said[0].read_text(encoding="utf-8"))
    assert "The chart for this visit is missing." in re.sub(r"\s+", " ", text)


def test_the_archive_counts_a_chart_whose_copy_failed(
    records: list[PatientRecord], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other way a chart fails to arrive: it is there, and the copy fails.

    A full disk or a revoked permission mid-run. The deliverer already warned
    and carried on, which is right — the other several hundred charts still
    have to be filed — but the chart is just as absent as a deleted one.
    """
    import anastomosis.deliver._shared as shared

    charts = tmp_path / "charts"
    entries = _charts(records, charts)
    doomed = entries[0].pdf
    real = shared.atomic_copy

    def refuse(source: Path, destination: Path) -> None:
        if source.name == doomed:
            raise OSError(28, "No space left on device")
        real(source, destination)

    monkeypatch.setattr(shared, "atomic_copy", refuse)
    result = ArchiveDeliverer().deliver(records, charts, tmp_path / "archive")

    assert result.missing_count == 1
    assert result.pdf_count == len(entries) - 1
    pages = sorted((tmp_path / "archive").rglob("encounters/*.html"))
    said = [p for p in pages if "missing-chart" in p.read_text(encoding="utf-8")]
    assert len(said) == 1


def test_a_chart_the_sweep_rescues_is_not_also_called_missing(
    records: list[PatientRecord], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One chart, one count.

    A failed attributed copy leaves the chart unclaimed, so the unattributed
    sweep picks it up — and usually succeeds, because the failure was
    transient. The chart is then IN the archive, in ``unattributed/``. Counting
    it at both places said "1 missing, 1 unattributed" about a single chart
    that was not lost at all.
    """
    import anastomosis.deliver._shared as shared

    charts = tmp_path / "charts"
    entries = _charts(records, charts)
    doomed = entries[0].pdf
    real = shared.atomic_copy
    attempts = 0

    def fail_once(source: Path, destination: Path) -> None:
        nonlocal attempts
        if source.name == doomed:
            attempts += 1
            if attempts == 1:
                raise OSError(28, "No space left on device")
        real(source, destination)

    monkeypatch.setattr(shared, "atomic_copy", fail_once)
    result = ArchiveDeliverer().deliver(records, charts, tmp_path / "archive")

    assert (result.missing_count, result.unattributed_count) == (0, 1)
    assert len(list((tmp_path / "archive" / "unattributed").glob("*.pdf"))) == 1


def test_an_encounter_that_was_never_rendered_says_nothing(
    records: list[PatientRecord], tmp_path: Path
) -> None:
    """The negative half, and the reason the page needs the encounter id.

    "No chart link" means two different things. A visit a selection rule kept
    out of the render has no chart and never had one; telling its reader that a
    chart is missing would be a false alarm on every excluded encounter, which
    is how a real alarm stops being read.
    """
    charts = tmp_path / "charts"
    entries = _charts(records, charts)
    # Not rendered at all: no file AND no index row, which is what an excluded
    # encounter looks like on disk.
    (charts / entries[0].pdf).unlink()
    RenderIndex.from_entries(entries[1:]).write(charts)

    result = ArchiveDeliverer().deliver(records, charts, tmp_path / "archive")

    assert result.missing_count == 0
    pages = sorted((tmp_path / "archive").rglob("encounters/*.html"))
    assert not [p for p in pages if "missing-chart" in p.read_text(encoding="utf-8")]


def test_the_bundle_counts_a_chart_the_index_names_and_that_is_not_there(
    records: list[PatientRecord], tmp_path: Path
) -> None:
    """The bundle filtered these away inside a comprehension — no count, no log."""
    charts = tmp_path / "charts"
    entries = _charts(records, charts)
    (charts / entries[0].pdf).unlink()

    written = BundleDeliverer().deliver_records(records, charts, tmp_path / "bundles")

    assert sum(b.missing_count for b in written) == 1
    owner = [b for b in written if b.patient_id == entries[0].patient_id]
    assert [b.missing_count for b in owner] == [1], "counted against the right patient"


def test_an_ordinary_delivery_reports_nothing_lost(
    records: list[PatientRecord], tmp_path: Path
) -> None:
    """Every run that goes right has to stay quiet, or none of the above is read."""
    charts = tmp_path / "charts"
    entries = _charts(records, charts)

    archive = ArchiveDeliverer().deliver(records, charts, tmp_path / "archive")
    bundles = BundleDeliverer().deliver_records(records, charts, tmp_path / "bundles")

    assert (archive.missing_count, archive.unattributed_count) == (0, 0)
    assert archive.pdf_count == len(entries)
    assert sum(b.missing_count for b in bundles) == 0
    pages = sorted((tmp_path / "archive").rglob("encounters/*.html"))
    assert not [p for p in pages if "missing-chart" in p.read_text(encoding="utf-8")]


# --- the two surfaces the counts exist for -----------------------------------


def _cli_lines(kind: str, **counts: int) -> list[str]:
    """What ``_print_delivery`` puts on screen for one outcome."""
    from rich.console import Console

    import anastomosis.cli as cli
    from anastomosis.core.commands import DeliveryOutcome

    recorder = Console(record=True, width=200, no_color=True)
    original, cli.console = cli.console, recorder
    try:
        cli._print_delivery(DeliveryOutcome(kind=kind, out_dir=Path("out"), counts=counts))
    finally:
        cli.console = original
    return [line.strip() for line in recorder.export_text().splitlines() if line.strip()]


def test_the_cli_says_what_the_delivery_could_not_file() -> None:
    lines = _cli_lines("archive", patients=3, encounters=6, pdfs=5, missing=1, unattributed=2)
    assert len(lines) == 3, lines
    assert lines[0].startswith("Archive: 3 patients")
    assert "1 chart this run rendered is missing from the archive" in lines[1]
    assert "2 charts could not be matched to a patient" in lines[2]
    assert "unattributed/" in lines[2]


def test_the_cli_says_nothing_when_the_delivery_lost_nothing() -> None:
    """The whole line budget of a good run is one line, exactly as before."""
    assert (
        len(_cli_lines("archive", patients=3, encounters=6, pdfs=6, missing=0, unattributed=0)) == 1
    )
    assert len(_cli_lines("bundle", patients=3, missing=0)) == 1
    assert len(_cli_lines("ccda", patients=3, missing=0)) == 1


def test_the_cli_says_a_patient_has_no_ccda_document() -> None:
    """The C-CDA export's shortfall is a different one from the archive's.

    Nothing was misfiled and there is no charts folder to check — a patient
    simply has no document, and the export the operator is about to hand to
    another EHR is short by that patient. Saying "1 chart is missing from it"
    would send them looking in the wrong place for a file that was never built.
    """
    (line,) = _cli_lines("ccda", patients=2, missing=1)[1:]
    assert "1 patient has no C-CDA document" in line
    assert "the export is incomplete" in line
    assert "charts folder" not in line

    (plural,) = _cli_lines("ccda", patients=1, missing=2)[1:]
    assert "2 patients have no C-CDA document" in plural


def test_the_cli_shortfall_names_no_chart() -> None:
    """PHI: a chart filename carries a patient's name and their date of service.

    Nothing here may grow one. The counts are the whole vocabulary.
    """
    lines = _cli_lines("archive", patients=1, encounters=1, pdfs=0, missing=1, unattributed=1)
    assert not [line for line in lines if ".pdf" in line]


def _deliver_events(kind: str = "archive", **counts: int) -> list[dict[str, object]]:
    """The events the GUI's deliver rail receives for one delivery outcome."""
    from anastomosis.core.commands import DeliveryOutcome
    from anastomosis.gui.consoles.runs import PipelineConsole, SummaryStore

    class _Jobs:
        def acquire(self) -> bool:
            return True

        def release(self) -> None:
            pass

    emitted: list[dict[str, object]] = []
    console = PipelineConsole(emitted.append, _Jobs(), SummaryStore())
    outcome = DeliveryOutcome(kind=kind, out_dir=Path("out"), counts=counts)
    console._present_deliveries({kind: outcome}, {})
    return [e for e in emitted if e.get("type") == "progress"]


def test_the_gui_deliver_rail_carries_the_shortfall() -> None:
    """The GUI showed only ``patients``, so both numbers were invisible there."""
    (event,) = _deliver_events(patients=3, encounters=6, pdfs=5, missing=1, unattributed=2)
    assert event["missing"] == 1
    assert event["unattributed"] == 2


def test_the_gui_deliver_rail_stays_short_when_nothing_was_lost() -> None:
    (event,) = _deliver_events(patients=3, encounters=6, pdfs=6, missing=0, unattributed=0)
    assert "missing" not in event and "unattributed" not in event


def test_the_gui_deliver_rail_carries_the_ccda_shortfall() -> None:
    """The rail reads ``missing`` off whatever counts an outcome carries, so the
    C-CDA export reaches it the moment the deliverer starts reporting one. This
    pins that: the rail was never the missing half, the count was.
    """
    (event,) = _deliver_events("ccda", patients=2, missing=1)
    assert event["missing"] == 1
    (quiet,) = _deliver_events("ccda", patients=3, missing=0)
    assert "missing" not in quiet
