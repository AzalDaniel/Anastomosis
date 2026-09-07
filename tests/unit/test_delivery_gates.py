"""The route a bundle was prepared for, and the gates it has to have
passed. Until schema v3 the manifest carried neither the chosen route
nor a record that the run's gates had passed, so an executor had
nothing to check against: a bundle rendered with ``--no-qa``, or one
whose charts were replaced after review, read exactly like a clean
one.

These tests pin both halves — what a run records, and what an
executor refuses. Synthetic throughout.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

import pytest

from anastomosis.core.model import Patient, PatientRecord
from anastomosis.deliver.browser.gates import (
    CONSERVATION_BALANCED,
    GATE_FAIL,
    GATE_NOT_RUN,
    GATE_PASS,
    DeliveryRefused,
    RoutePlan,
    RunGates,
    assert_deliverable,
    route_plan_of,
)
from anastomosis.deliver.browser.persist import (
    GATE_VERSION,
    LADDER_VERSION,
    MANIFEST_NAME,
    load_upload_manifest,
    write_upload_manifest,
)
from anastomosis.deliver.router import RouteKind, RouteOption, TransitMap
from anastomosis.reconstruct.engine import RenderedDoc

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"
PAT = "feedface-0000-0000-0000-0000000000a1"
ENC = "feedface-e000-0000-0000-0000000000a1"
CLEAN_GATES = RunGates(qa=GATE_PASS, conservation=CONSERVATION_BALANCED, layout_hash="a" * 64)
BROWSER_ROUTE = RoutePlan(destination="tebra", kind=RouteKind.BROWSER.value)


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


def _bundle(
    tmp_path: Path,
    *,
    route: RoutePlan | None = BROWSER_ROUTE,
    gates: RunGates | None = CLEAN_GATES,
) -> Path:
    """One rendered chart plus the manifest that describes it; returns
    the dir. The chart lands INSIDE the output directory, as a real
    render's does: the manifest stores a basename re-absolutized
    against that directory on read."""
    out = tmp_path / "charts"
    out.mkdir()
    chart = out / "note.pdf"
    chart.write_bytes(b"chart-bytes")
    docs = [RenderedDoc(path=chart, encounter_id=ENC, patient_id=PAT)]
    records = [
        PatientRecord(
            id=PAT,
            patient=Patient(
                id=PAT,
                family_name="Fixture",
                given_name="Ada",
                birth_date=datetime.date(1980, 3, 14),
            ),
        )
    ]
    write_upload_manifest(docs, records, out, pack="generic_soap", route=route, gates=gates)
    return out


# --- what a run records -------------------------------------------------------


def test_the_manifest_carries_the_route_and_the_gates(tmp_path: Path) -> None:
    out = _bundle(tmp_path)

    data = json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))

    # A bundle of rendered charts with a gate record IS a version-3 file, and
    # says so even now that 4 exists: the version describes the CONTENT, and
    # nothing here carries a source document.
    assert data["version"] == GATE_VERSION == 3
    assert data["route"] == {"destination": "tebra", "kind": "browser"}
    assert data["gates"] == {
        "qa": "pass",
        "conservation": "balanced",
        "layout_hash": "a" * 64,
    }


def test_they_round_trip_back_off_disk(tmp_path: Path) -> None:
    out = _bundle(tmp_path)

    manifest = load_upload_manifest(out)

    assert manifest.route == BROWSER_ROUTE
    assert manifest.gates == CLEAN_GATES


def test_a_v2_manifest_keeps_its_ladder_fields(tmp_path: Path) -> None:
    """The version-gating trap the reader walked into the moment v3 existed:
    it decided "does this file have the ladder fields?" by comparing against
    the CURRENT version, so bumping the current version would have silently
    dropped v2's pack, page counts and dates of service."""
    out = _bundle(tmp_path)
    path = out / MANIFEST_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = LADDER_VERSION
    del data["route"], data["gates"]
    path.write_text(json.dumps(data), encoding="utf-8")

    manifest = load_upload_manifest(out)

    assert manifest.version == LADDER_VERSION
    assert manifest.pack == "generic_soap"  # the v2 ladder field survived
    assert manifest.encounters[ENC].date_of_service is None
    assert manifest.route is None and manifest.gates is None


def test_route_plan_of_reads_a_transit_map() -> None:
    viable = RouteOption(kind=RouteKind.BROWSER, viable=True, why="a taught browser route")
    transit = TransitMap(destination="tebra", options=(viable,), chosen=viable)

    assert route_plan_of(transit) == BROWSER_ROUTE


def test_a_transit_map_with_no_route_records_that_too() -> None:
    """A capability gap is a fact to record, not a field to leave blank."""
    option = RouteOption(kind=RouteKind.BROWSER, viable=False, why="no destination pack")
    transit = TransitMap(destination="tebra", options=(option,), chosen=None)

    plan = route_plan_of(transit)

    assert plan == RoutePlan(destination="tebra", kind=None)
    assert plan.viable is False


# --- what an executor refuses -------------------------------------------------


def test_a_clean_bundle_is_deliverable(tmp_path: Path) -> None:
    assert_deliverable(load_upload_manifest(_bundle(tmp_path)))


def test_a_failed_qa_gate_refuses(tmp_path: Path) -> None:
    gates = RunGates(qa=GATE_FAIL, conservation=CONSERVATION_BALANCED, layout_hash="a" * 64)
    manifest = load_upload_manifest(_bundle(tmp_path, gates=gates))

    with pytest.raises(DeliveryRefused, match="QA failed"):
        assert_deliverable(manifest)


def test_an_unverified_bundle_refuses_and_names_the_remedy(tmp_path: Path) -> None:
    """``--no-qa`` is an operator's choice about rendering. It is not a choice
    to file unverified charts into somebody's chart hours later."""
    gates = RunGates(qa=GATE_NOT_RUN, conservation=CONSERVATION_BALANCED, layout_hash=None)
    manifest = load_upload_manifest(_bundle(tmp_path, gates=gates))

    with pytest.raises(DeliveryRefused) as caught:
        assert_deliverable(manifest)

    assert "never verified" in str(caught.value)
    assert "re-render with QA on" in str(caught.value)


def test_an_unbalanced_seam_refuses(tmp_path: Path) -> None:
    gates = RunGates(qa=GATE_PASS, conservation="unbalanced", layout_hash="a" * 64)
    manifest = load_upload_manifest(_bundle(tmp_path, gates=gates))

    with pytest.raises(DeliveryRefused, match="did not balance"):
        assert_deliverable(manifest)


def test_a_route_that_found_no_way_in_refuses(tmp_path: Path) -> None:
    manifest = load_upload_manifest(
        _bundle(tmp_path, route=RoutePlan(destination="tebra", kind=None))
    )

    with pytest.raises(DeliveryRefused, match="no viable automated route"):
        assert_deliverable(manifest)


def test_charts_replaced_after_review_refuse_the_whole_run(tmp_path: Path) -> None:
    """Not "skip that item": the bundle differs from the bundle that passed."""
    out = _bundle(tmp_path)
    manifest = load_upload_manifest(out)
    (out / "note.pdf").write_bytes(b"different-bytes-entirely")

    with pytest.raises(DeliveryRefused) as caught:
        assert_deliverable(manifest)

    assert "1 of 1 chart(s) no longer match" in str(caught.value)


def test_a_chart_that_vanished_refuses(tmp_path: Path) -> None:
    out = _bundle(tmp_path)
    manifest = load_upload_manifest(out)
    (out / "note.pdf").unlink()

    with pytest.raises(DeliveryRefused, match="no longer match"):
        assert_deliverable(manifest)


def test_a_genuinely_older_bundle_warns_but_is_not_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Operators have rendered trees on disk; stranding them would be
    worse than the failure being fixed — but never silently. A
    GENUINELY older manifest declares a version before gates existed,
    distinct from a current-version file with gates stripped (the
    bypass the sibling test below covers)."""
    out = _bundle(tmp_path)
    path = out / MANIFEST_NAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = GATE_VERSION - 1
    for key in ("route", "gates"):
        payload.pop(key, None)
    path.write_text(json.dumps(payload), encoding="utf-8")

    manifest = load_upload_manifest(out)
    with caplog.at_level(logging.WARNING):
        assert_deliverable(manifest)

    assert "records no gate outcomes" in caplog.text


def test_a_current_manifest_with_its_gates_nulled_is_a_defect(tmp_path: Path) -> None:
    """The bypass the grandfather clause was accidentally covering:
    both writers always record route and gates, so a file that
    DECLARES this version with nulls was written incompletely or
    edited, not old — reading it as "no gate record" would hand an
    executor the one branch it is allowed to walk past."""
    out = _bundle(tmp_path)  # a real gated write, so the file really is v3
    path = out / MANIFEST_NAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == GATE_VERSION
    payload["gates"] = None
    path.write_text(json.dumps(payload), encoding="utf-8")

    # It still LOADS — a null is a legal shape at the read — but it may not buy
    # delivery, because a file that declares this version was written by a
    # writer that had gates to record.
    manifest = load_upload_manifest(out)
    with pytest.raises(DeliveryRefused, match="records no gate outcomes"):
        assert_deliverable(manifest)


def test_a_refusal_names_no_path_and_no_patient(tmp_path: Path) -> None:
    """A refusal is printed to a terminal and written to a log, both outside the
    hardened output directory. It may carry counts and gate names only."""
    out = _bundle(tmp_path)
    manifest = load_upload_manifest(out)
    (out / "note.pdf").write_bytes(b"tampered")

    with pytest.raises(DeliveryRefused) as caught:
        assert_deliverable(manifest)

    message = str(caught.value)
    for forbidden in ("note.pdf", str(out), PAT, ENC, "Fixture", "Ada", "1980"):
        assert forbidden not in message


# --- the executor, and the run that feeds it ---------------------------------


def test_the_executor_refuses_before_it_touches_the_destination(tmp_path: Path) -> None:
    """The refusal has to land BEFORE ``attach``: reaching a browser at all
    means a session was opened against a live EHR for a bundle that was never
    going to be filed."""
    from anastomosis.core.upload_command import UploadCommand, run_upload_command

    gates = RunGates(qa=GATE_FAIL, conservation=CONSERVATION_BALANCED, layout_hash=None)
    out = _bundle(tmp_path, gates=gates)

    def _attach() -> object:
        raise AssertionError("the destination must never be attached for a refused bundle")

    with pytest.raises(DeliveryRefused, match="QA failed"):
        run_upload_command(UploadCommand(out_dir=out, verify=False), _attach)


def test_a_render_run_records_its_own_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the gates in the manifest are the gates the run actually
    passed, and the layout hash is the one the provenance record published."""
    pytest.importorskip("pymupdf", reason="the fake renderer writes a real PDF")
    import anastomosis.reconstruct.chromium as chromium
    import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
    from anastomosis.core.commands import PipelineCommand, run_pipeline_command
    from anastomosis.reconstruct.provenance import RENDER_PROVENANCE_NAME

    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    charts = tmp_path / "charts"
    run_pipeline_command(
        PipelineCommand(
            export_dir=FIXTURE,
            charts_dir=charts,
            source="pf-tebra",
            qa=False,
            write_manifest=True,
            route=BROWSER_ROUTE,
        )
    )

    manifest = load_upload_manifest(charts)
    provenance = json.loads((charts / RENDER_PROVENANCE_NAME).read_text(encoding="utf-8"))

    assert manifest.route == BROWSER_ROUTE
    assert manifest.gates is not None
    # QA was switched off, so this bundle is unverified and says so — which is
    # exactly what an executor refuses on.
    assert manifest.gates.qa == GATE_NOT_RUN
    assert manifest.gates.layout_hash == provenance["content_hash"]
    with pytest.raises(DeliveryRefused, match="never verified"):
        assert_deliverable(manifest)
