"""Tests for the reconstruction engine: the PF fixture rendered through the
built-in generic_soap pack with a fake renderer — the whole pipeline except
Chromium itself."""

from datetime import date
from pathlib import Path

import pytest

import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
from anastomosis.core.model import Encounter, Patient, PatientRecord
from anastomosis.deliver.render_index import RenderIndex
from anastomosis.reconstruct import LoadedPack, discover_packs
from anastomosis.reconstruct.engine import ReconstructionEngine
from anastomosis.sources import get_source

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"


class FakeRenderer:
    def __init__(self, log: list[tuple[str, Path]]) -> None:
        self.log = log
        self.closed = False

    def render(self, html: str, pdf_path: Path) -> None:
        pdf_path.write_bytes(b"%PDF-1.7 fake")
        self.log.append((html, pdf_path))

    def close(self) -> None:
        self.closed = True


class FakeFactory:
    def __init__(self) -> None:
        self.log: list[tuple[str, Path]] = []
        self.instances: list[FakeRenderer] = []

    def __call__(self) -> FakeRenderer:
        renderer = FakeRenderer(self.log)
        self.instances.append(renderer)
        return renderer


@pytest.fixture(scope="module")
def records() -> list[PatientRecord]:
    return list(get_source("pf-tebra").load(FIXTURE))


@pytest.fixture(scope="module")
def pack() -> LoadedPack:
    status = discover_packs()["generic_soap"]
    assert status.pack is not None, status.diagnosis
    return status.pack


def _engine(pack: LoadedPack, factory: FakeFactory, **kwargs: object) -> ReconstructionEngine:
    return ReconstructionEngine(pack, factory, **kwargs)  # type: ignore[arg-type]


def test_builtin_pack_discovered_without_opt_in(pack: LoadedPack) -> None:
    assert pack.manifest.name == "generic_soap"
    assert pack.manifest.sections["insurance"].default is False


def test_renders_all_encounters_with_collision_suffix(
    records: list[PatientRecord], pack: LoadedPack, tmp_path: Path
) -> None:
    factory = FakeFactory()
    result = _engine(pack, factory).run(records, tmp_path / "out")
    assert [len(result.rendered), len(result.skipped), len(result.failed)] == [6, 0, 0]
    names = sorted(p.name for p in result.rendered)
    same_day = [n for n in names if n.startswith("Fixture_Ada_05-10-2023")]
    assert len(same_day) == 2
    assert "Fixture_Ada_05-10-2023.pdf" in same_day
    assert any(n.startswith("Fixture_Ada_05-10-2023-feedface") for n in same_day)
    assert all(p.read_bytes().startswith(b"%PDF") for p in result.rendered)


def _same_day_record(*encounter_ids: str) -> PatientRecord:
    """One patient, one day, one note type — so only the id can separate the
    charts. The ids given share their first eight characters, which is the
    ordinary case for sequential source ids, not a contrived one."""
    return PatientRecord(
        patient=Patient(
            id="feedface-0000-0000-0000-000000000001",
            given_name="Sameday",
            family_name="Fixture",
        ),
        encounters=[
            Encounter(
                id=eid,
                patient_id="feedface-0000-0000-0000-000000000001",
                date_of_service=date(2023, 5, 10),
                note_type="SOAP",
            )
            for eid in encounter_ids
        ],
    )


#: Three ids agreeing on the first eight characters after the hyphens come
#: out — the width the allocator suffixes with.
_PREFIX_TWINS = (
    "feedface-0000-0000-0000-00000000000a",
    "feedface-0000-0000-0000-00000000000b",
    "feedface-0000-0000-0000-00000000000c",
)


def test_same_day_charts_never_share_a_file(pack: LoadedPack, tmp_path: Path) -> None:
    """Every encounter gets its own chart, however alike the ids are: the
    allocator must check the suffixed name is free too, not just apply a
    fixed id-prefix suffix once. Prefix-identical ids are the normal
    case, not a corner: sequential Millennium ``ENCNTR_ID``s share long
    prefixes, as does every ``feedface`` GUID in this repo's fixtures."""
    record = _same_day_record(*_PREFIX_TWINS)
    result = _engine(pack, FakeFactory()).run([record], tmp_path / "out")

    assert len(result.failed) == 0
    assert len(result.rendered) == len(_PREFIX_TWINS), "an encounter rendered nowhere"
    assert len({p.name for p in result.rendered}) == len(_PREFIX_TWINS), (
        "two encounters share one file — one chart was overwritten"
    )
    # The count in the report and the files on disk must be the same number.
    assert len(list((tmp_path / "out").glob("*.pdf"))) == len(_PREFIX_TWINS)


def test_the_render_index_maps_each_chart_to_exactly_one_encounter(
    pack: LoadedPack, tmp_path: Path
) -> None:
    """The sidecar exists so a chart is never misattributed, so it must
    be injective — one filename, one encounter — never a last-wins merge
    that could turn an overwrite into a clean-looking run."""
    out = tmp_path / "out"
    record = _same_day_record(*_PREFIX_TWINS)
    _engine(pack, FakeFactory()).run([record], out)

    index = RenderIndex.load(out)
    assert index is not None, "no index written"
    assert len(index.entries) == len(_PREFIX_TWINS)
    assert len({e.pdf for e in index.entries}) == len(_PREFIX_TWINS)
    assert {e.encounter_id for e in index.entries} == set(_PREFIX_TWINS)


def test_two_encounters_sharing_an_id_still_get_a_chart_each(
    pack: LoadedPack, tmp_path: Path
) -> None:
    """A C-CDA legitimately lists two visits under one ``<id root>``, so this
    is ordinary input. Two names are available before the id is needed to tell
    them apart — the bare one and the short-suffix one — so both charts are
    written and neither is lost."""
    shared = "feedface-0000-0000-0000-0000000000ff"
    result = _engine(pack, FakeFactory()).run([_same_day_record(shared, shared)], tmp_path / "out")

    assert len(result.failed) == 0
    assert len({p.name for p in result.rendered}) == 2


def test_a_chart_that_cannot_be_named_is_reported_not_overwritten(
    pack: LoadedPack, tmp_path: Path
) -> None:
    """When the id itself stops separating charts, the run says so: the
    allocator can build three names from one id (bare, short suffix, full
    suffix), so a FOURTH encounter on that id exhausts them, and a chart
    nothing can name must be a reported failure, not silently dropped —
    while the rest of the batch still renders."""
    shared = "feedface-0000-0000-0000-0000000000ff"
    record = _same_day_record(
        shared, shared, shared, shared, "feedface-0000-0000-0000-0000000000fe"
    )
    result = _engine(pack, FakeFactory()).run([record], tmp_path / "out")

    assert len(result.failed) == 1, f"a chart went missing quietly: {result.failed}"
    assert result.failed[0] == (shared, "DuplicateEncounterId")
    # Every chart that COULD be named still was, each in its own file.
    assert len(result.rendered) == 4
    assert len({p.name for p in result.rendered}) == 4


def test_rerun_is_idempotent(
    records: list[PatientRecord], pack: LoadedPack, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    factory = FakeFactory()
    first = _engine(pack, factory).run(records, out)
    second = _engine(pack, FakeFactory()).run(records, out)
    assert len(first.rendered) == 6
    assert len(second.rendered) == 0
    assert sorted(second.skipped) == sorted(first.rendered)
    # Skipped is not unverified: the QA manifest still lists every document,
    # so re-runs re-check files that already exist on disk.
    assert len(second.documents) == 6


def test_renderer_recycling(records: list[PatientRecord], pack: LoadedPack, tmp_path: Path) -> None:
    factory = FakeFactory()
    _engine(pack, factory, recycle_every=2).run(records, tmp_path / "out")
    # 6 renders at 2 per renderer = 3 instances, each closed.
    assert len(factory.instances) == 3
    assert all(r.closed for r in factory.instances)


def test_crash_relaunch_costs_one_retry_not_the_batch(
    records: list[PatientRecord], pack: LoadedPack, tmp_path: Path
) -> None:
    factory = FakeFactory()

    class CrashOnce(FakeRenderer):
        crashed = False

        def render(self, html: str, pdf_path: Path) -> None:
            if not CrashOnce.crashed:
                CrashOnce.crashed = True
                raise RuntimeError("chromium went away")
            super().render(html, pdf_path)

    def crashing_factory() -> FakeRenderer:
        renderer = CrashOnce(factory.log)
        factory.instances.append(renderer)
        return renderer

    engine = _engine(pack, factory)
    engine._factory = crashing_factory  # type: ignore[attr-defined]
    result = engine.run(records, tmp_path / "out")
    assert len(result.rendered) == 6
    assert result.failed == []
    assert len(factory.instances) == 2  # crashed one + relaunched one


def test_section_flags_control_rendered_content(
    records: list[PatientRecord], pack: LoadedPack, tmp_path: Path
) -> None:
    ada = [r for r in records if r.patient.family_name == "Fixture"]

    default_factory = FakeFactory()
    _engine(pack, default_factory).run(ada, tmp_path / "default")
    default_html = "\n".join(html for html, _ in default_factory.log)
    assert "Payment information" not in default_html  # insurance defaults off
    assert "Addenda" in default_html  # addenda defaults on
    assert "lipid panel" in default_html

    insured_factory = FakeFactory()
    _engine(pack, insured_factory, section_overrides={"insurance": True, "addenda": False}).run(
        ada, tmp_path / "insured"
    )
    insured_html = "\n".join(html for html, _ in insured_factory.log)
    assert "Payment information" in insured_html
    assert "Cascadia Choice (PPO)" in insured_html
    assert "Addenda" not in insured_html


def test_rendered_chart_content(
    records: list[PatientRecord], pack: LoadedPack, tmp_path: Path
) -> None:
    factory = FakeFactory()
    _engine(pack, factory).run(records, tmp_path / "out")

    # The engine renders to a sibling temp ".{final}.{pid}.tmp" then atomically
    # replaces the target, so recover the final name the renderer stood in for.
    def _final(path: Path) -> str:
        return path.name[1:].rsplit(".", 2)[0] if path.name.endswith(".tmp") else path.name

    by_file = {_final(path): html for html, path in factory.log}

    ada_note = by_file["Fixture_Ada_05-10-2023.pdf"]
    assert "Ada Q Fixture" in ada_note
    assert "DOB 03/14/1985" in ada_note
    assert "Example Family Medicine" in ada_note
    assert "<p>Reports good medication adherence" in ada_note  # source HTML verbatim
    assert "Body mass index" in ada_note and "25.7" in ada_note  # auto-BMI charted
    assert "Electronically signed by Paige Providerson" in ada_note

    well_child = by_file["Placeholder_Cleo_04-18-2023.pdf"]
    assert "UNSIGNED NOTE" in well_child
    assert "(16 mos)" in well_child  # pediatric age in months, no tz shift


def test_output_dir_is_hardened(
    records: list[PatientRecord], pack: LoadedPack, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    _engine(pack, FakeFactory()).run(records, out)
    assert (out / "_PHI_WARNING_README.txt").exists()
