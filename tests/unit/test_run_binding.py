"""Destination-bound runs: immutable profiles, the run manifest, and the refusal.

The binding only earns its keep if it BREAKS. So the centre of this file is
three tests that each change exactly one input under a prepared output folder —
a pack's bytes, a learned mapping's bytes, the destination's declared version —
and prove the next run into that folder refuses and names the profile that
moved. A test that only walks the happy path has not tested a refusal.

Around those: the profiles are frozen and content-addressed, the manifest is
deterministic and PHI-free, and the state machine refuses an unbound folder, a
drifted one, and an illegal move.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from _render_fakes import write_text_pdf

import anastomosis.reconstruct.chromium as chromium
from anastomosis.core.migrate import MigrationCommand, resolve_pack, run_migration
from anastomosis.core.profiles import (
    SOURCE_BUILTIN,
    SOURCE_LEARNED,
    DestinationCapability,
    DestinationProfile,
    LayoutProfile,
    ProfileError,
    RunBinding,
    SourceProfile,
    capture_binding,
    capture_destination_profile,
    capture_layout_profile,
    capture_source_profile,
)
from anastomosis.core.runmanifest import (
    RUN_MANIFEST_NAME,
    BindingError,
    RunManifest,
    RunManifestError,
    RunState,
    RunStateError,
    advance_state,
    export_dir_id,
    load_run_manifest,
    read_run_manifest,
    recapture_binding,
    run_manifest_path,
    verify_binding,
    write_run_manifest,
)
from anastomosis.core.source_init_command import SourceInitCommand, run_source_init_command
from anastomosis.destinations.registry import UNVERSIONED, DestinationRegistry
from anastomosis.pipeline import PipelineError

if TYPE_CHECKING:
    from anastomosis.core.profiles import RunBinding as _RunBinding

PF_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"
CSV_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "learned" / "clinic_visits.csv"
BUILTIN_PACKS = Path(__import__("anastomosis").__file__).resolve().parent / "packs"


class _FakeChromium:
    """Writes a real PDF carrying the chart text — the ``test_migrate`` pattern."""

    def __init__(self, **kwargs: object) -> None:
        pass

    def render(self, html: str, pdf_path: Path) -> None:
        write_text_pdf(html, pdf_path)

    def close(self) -> None:
        pass


@pytest.fixture
def fake_chromium(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)


def _bump_destination_version(monkeypatch: pytest.MonkeyPatch, name: str, version: str) -> None:
    """Make the registry report ``name`` at a different product version.

    Patched on the class, so BOTH readers see it: ``plan_route``'s registry load
    inside ``run_migration`` and ``capture_destination_profile``'s. A version
    that moved for one and not the other would prove nothing.
    """
    base = DestinationRegistry.load()
    entry = base.entries[name].model_copy(update={"version": version})
    bumped = DestinationRegistry(entries={**base.entries, name: entry})
    monkeypatch.setattr(DestinationRegistry, "load", classmethod(lambda cls, path=None: bumped))


def _teach_clinic_csv(destination: str | None = None) -> None:
    """Teach the flat clinic CSV as a learned source in this test's fake home."""
    result = run_source_init_command(
        SourceInitCommand(
            example=CSV_FIXTURE,
            name="clinic_csv",
            display="Clinic CSV",
            confirmed=True,
            destination=destination,
        )
    )
    assert result.ok is True, result.error


def _csv_export_dir(tmp_path: Path) -> Path:
    export = tmp_path / "export"
    export.mkdir()
    shutil.copy(CSV_FIXTURE, export / "clinic_visits.csv")
    return export


# --- the profiles are immutable and content-addressed -------------------------


def test_profiles_are_frozen() -> None:
    profile = SourceProfile(name="pf-tebra", kind=SOURCE_BUILTIN)
    with pytest.raises(AttributeError):
        profile.name = "something-else"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changed", "field"),
    [
        (SourceProfile(name="other", kind=SOURCE_BUILTIN), "name"),
        (SourceProfile(name="pf-tebra", kind=SOURCE_LEARNED), "kind"),
        (SourceProfile(name="pf-tebra", kind=SOURCE_BUILTIN, mapping_sha256="ab"), "mapping"),
        (
            SourceProfile(name="pf-tebra", kind=SOURCE_BUILTIN, taught_for_destination="epic"),
            "taught_for",
        ),
    ],
)
def test_every_source_field_moves_the_hash(changed: SourceProfile, field: str) -> None:
    """No field is decorative: each one is part of the address, so a change shows."""
    base = SourceProfile(name="pf-tebra", kind=SOURCE_BUILTIN)
    assert changed.profile_hash != base.profile_hash, field


def test_profile_hash_is_stable_and_kind_separated() -> None:
    a = LayoutProfile(render_mode="neutral", pack="generic_soap", content_hash="ff")
    b = LayoutProfile(render_mode="neutral", pack="generic_soap", content_hash="ff")
    assert a.profile_hash == b.profile_hash
    # Domain separation: two profiles of different KINDS never collide even when
    # nothing but the kind distinguishes their payloads.
    assert (
        LayoutProfile(render_mode="x").profile_hash
        != SourceProfile(name="x", kind="x").profile_hash
    )


def test_profile_round_trips_through_json() -> None:
    binding = RunBinding(
        source=SourceProfile(name="s", kind=SOURCE_BUILTIN),
        destination=DestinationProfile(
            name="d",
            display="D",
            version=UNVERSIONED,
            capabilities=(DestinationCapability("browser", "pack", "d"),),
        ),
        layout=LayoutProfile(render_mode="neutral", pack="p", content_hash="ab"),
    )
    again = RunBinding.from_json(json.loads(json.dumps(binding.to_json())))
    assert again == binding
    assert again.binding_hash == binding.binding_hash


def test_capture_refuses_an_unknown_source_or_destination() -> None:
    with pytest.raises(ProfileError, match="unknown source"):
        capture_source_profile("no-such-adapter")
    with pytest.raises(ProfileError, match="unknown destination"):
        capture_destination_profile("no-such-destination")


def test_layout_profile_reuses_the_pack_content_hash() -> None:
    """The layout hash is the pack-trust digest, not a second definition of it."""
    from anastomosis.reconstruct.packtrust import pack_content_hash

    profile = capture_layout_profile("neutral", "generic_soap")
    assert profile.content_hash == pack_content_hash(BUILTIN_PACKS / "generic_soap")
    assert profile.origin == "builtin"
    # ccda-standard renders through no Jinja pack: a truthful None, not a gap.
    assert capture_layout_profile("ccda-standard", None).content_hash is None


def test_source_profile_reuses_the_learned_mapping_digest(tmp_path: Path) -> None:
    """A learned source's address is the digest ``source_trust.json`` records."""
    _teach_clinic_csv()
    home_mapping = Path.home() / ".anastomosis" / "sources" / "clinic_csv"
    recorded = json.loads((home_mapping / "source_trust.json").read_text(encoding="utf-8"))
    profile = capture_source_profile("clinic_csv")
    assert profile.kind == SOURCE_LEARNED
    assert profile.mapping_sha256 == recorded["mapping_sha256"]


# --- the destination carries a version ----------------------------------------


def test_registry_entries_declare_a_version_explicitly() -> None:
    """Every shipped entry states a version — "unversioned" is a value, not a gap."""
    registry = DestinationRegistry.load()
    assert registry.entries
    for entry in registry.entries.values():
        assert entry.version == UNVERSIONED
    # And an entry MAY declare a real one; it changes the profile address.
    plain = capture_destination_profile("tebra", registry)
    bumped_entry = registry.entries["tebra"].model_copy(update={"version": "2026.2"})
    bumped = capture_destination_profile(
        "tebra", DestinationRegistry(entries={**registry.entries, "tebra": bumped_entry})
    )
    assert bumped.version == "2026.2"
    assert bumped.profile_hash != plain.profile_hash


def test_re_verifying_evidence_does_not_break_a_binding() -> None:
    """A bumped ``verified`` date is not a changed capability, so it is not drift."""
    registry = DestinationRegistry.load()
    entry = registry.entries["tebra"]
    assert entry.ccda_import.evidence is not None
    later = entry.ccda_import.evidence.model_copy(update={"verified": "2030-01-01"})
    fresher = entry.ccda_import.model_copy(update={"evidence": later})
    revised = entry.model_copy(update={"ccda_import": fresher})
    assert (
        capture_destination_profile(
            "tebra", DestinationRegistry(entries={**registry.entries, "tebra": revised})
        ).profile_hash
        == capture_destination_profile("tebra", registry).profile_hash
    )


# --- the run manifest ---------------------------------------------------------


def _manifest_for(tmp_path: Path, binding: _RunBinding) -> RunManifest:
    return RunManifest(
        pipeline_version="0.0.0-test",
        source="pf-tebra",
        destination="tebra",
        render_mode="neutral",
        export_dir_id=export_dir_id(tmp_path / "export"),
        binding=binding,
    )


def test_manifest_write_is_deterministic_and_owner_only(tmp_path: Path) -> None:
    binding = capture_binding(
        source="pf-tebra", destination="tebra", render_mode="neutral", pack="generic_soap"
    )
    manifest = _manifest_for(tmp_path, binding)
    out = tmp_path / "out"
    first = write_run_manifest(out, manifest).read_bytes()
    second = write_run_manifest(out, manifest).read_bytes()
    assert first == second  # no clock, no ordering churn
    assert read_run_manifest(out) == manifest
    if os.name == "posix":
        # atomic_write_text documents `mode` as POSIX-only, and Windows has no
        # bit to honour it with. The house idiom is to ask where the question
        # means something rather than to assert a mode nothing sets.
        assert stat.S_IMODE((out / RUN_MANIFEST_NAME).stat().st_mode) == 0o600


def test_unreadable_manifest_raises_but_an_absent_one_is_simply_unbound(
    tmp_path: Path,
) -> None:
    assert load_run_manifest(tmp_path) is None  # never bound: proceed, do not refuse
    run_manifest_path(tmp_path).write_text("{not json", encoding="utf-8")
    with pytest.raises(RunManifestError):
        load_run_manifest(tmp_path)  # bound, and we cannot tell to what: refuse


def test_unsupported_manifest_version_refuses(tmp_path: Path) -> None:
    binding = capture_binding(
        source="pf-tebra", destination="tebra", render_mode="neutral", pack="generic_soap"
    )
    payload = _manifest_for(tmp_path, binding).to_json()
    payload["version"] = 99
    run_manifest_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RunManifestError, match="version 99"):
        read_run_manifest(tmp_path)


# --- BREAK THE BINDING: three inputs, three named refusals ---------------------


def test_break_the_layout_rerun_refuses_naming_the_layout(
    tmp_path: Path, fake_chromium: None
) -> None:
    """Edit a pack's bytes; the next run into the same folder names ``layout``."""
    pytest.importorskip("pymupdf", reason="needs PyMuPDF")
    packs = tmp_path / "packs"
    packs.mkdir()
    shutil.copytree(BUILTIN_PACKS / "generic_soap", packs / "generic_soap")
    out = tmp_path / "out"
    cmd = MigrationCommand(
        export_dir=PF_FIXTURE,
        out_dir=out,
        source="pf-tebra",
        destination="tebra",
        render="generic_soap",
        pack_dirs=(packs,),
        trust_new=True,
        force=True,
    )
    run_migration(cmd)
    bound = read_run_manifest(out)
    assert bound.state is RunState.PREPARED
    assert bound.binding.layout.content_hash is not None

    template = packs / "generic_soap" / "template.html"
    template.write_text(template.read_text(encoding="utf-8") + "\n<!-- edited -->\n", "utf-8")

    with pytest.raises(PipelineError) as caught:
        run_migration(cmd)
    assert caught.value.kind == "binding_changed"
    message = str(caught.value)
    assert "layout profile changed" in message
    assert "source profile changed" not in message
    assert "destination profile changed" not in message
    # The refusal happened BEFORE anything was written: the folder still names
    # the layout it was actually rendered under.
    assert read_run_manifest(out) == bound


def test_break_the_mapping_rerun_refuses_naming_the_source(
    tmp_path: Path, fake_chromium: None
) -> None:
    """Hand-edit a learned mapping; the next run names ``source``."""
    pytest.importorskip("pymupdf", reason="needs PyMuPDF")
    _teach_clinic_csv()
    out = tmp_path / "out"
    cmd = MigrationCommand(
        export_dir=_csv_export_dir(tmp_path),
        out_dir=out,
        source="clinic_csv",
        destination="tebra",
        force=True,
    )
    run_migration(cmd)
    bound = read_run_manifest(out)
    assert bound.binding.source.kind == SOURCE_LEARNED

    mapping = Path.home() / ".anastomosis" / "sources" / "clinic_csv" / "mapping.json"
    spec = json.loads(mapping.read_text(encoding="utf-8"))
    spec["display"] = "Clinic CSV (edited after the run)"
    mapping.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(PipelineError) as caught:
        run_migration(cmd)
    assert caught.value.kind == "binding_changed"
    message = str(caught.value)
    assert "source profile changed" in message
    assert "layout profile changed" not in message
    assert read_run_manifest(out) == bound


def test_break_the_destination_rerun_refuses_naming_the_destination(
    tmp_path: Path, fake_chromium: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bump the destination's declared version; the next run names ``destination``."""
    pytest.importorskip("pymupdf", reason="needs PyMuPDF")
    out = tmp_path / "out"
    cmd = MigrationCommand(
        export_dir=PF_FIXTURE,
        out_dir=out,
        source="pf-tebra",
        destination="tebra",
        force=True,
    )
    run_migration(cmd)
    bound = read_run_manifest(out)
    assert bound.binding.destination.version == UNVERSIONED

    _bump_destination_version(monkeypatch, "tebra", "2027.1")

    with pytest.raises(PipelineError) as caught:
        run_migration(cmd)
    assert caught.value.kind == "binding_changed"
    message = str(caught.value)
    assert "destination profile changed" in message
    assert "source profile changed" not in message
    assert read_run_manifest(out) == bound

    # --rebind is the explicit way to say the earlier artifacts no longer stand.
    run_migration(
        MigrationCommand(
            export_dir=PF_FIXTURE,
            out_dir=out,
            source="pf-tebra",
            destination="tebra",
            force=True,
            rebind=True,
        )
    )
    rebound = read_run_manifest(out)
    assert rebound.binding.destination.version == "2027.1"
    assert rebound.binding_hash != bound.binding_hash


def test_an_unchanged_rerun_is_allowed(tmp_path: Path, fake_chromium: None) -> None:
    """The refusal is drift-triggered, not folder-triggered: nothing changed, so it runs."""
    pytest.importorskip("pymupdf", reason="needs PyMuPDF")
    out = tmp_path / "out"
    cmd = MigrationCommand(
        export_dir=PF_FIXTURE, out_dir=out, source="pf-tebra", destination="tebra", force=True
    )
    run_migration(cmd)
    first = (out / RUN_MANIFEST_NAME).read_bytes()
    run_migration(cmd)
    assert (out / RUN_MANIFEST_NAME).read_bytes() == first


# --- destination BEFORE teaching ----------------------------------------------


def test_a_mapping_taught_for_one_destination_refuses_another(tmp_path: Path) -> None:
    _teach_clinic_csv(destination="tebra")
    with pytest.raises(PipelineError) as caught:
        run_migration(
            MigrationCommand(
                export_dir=_csv_export_dir(tmp_path),
                out_dir=tmp_path / "out",
                source="clinic_csv",
                destination="epic",
            )
        )
    assert caught.value.kind == "destination_mismatch"
    message = str(caught.value)
    assert "'tebra'" in message and "'epic'" in message  # both ends named
    assert not (tmp_path / "out").exists()  # refused before anything was written


def test_a_mapping_refuses_the_same_destination_once_it_has_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _teach_clinic_csv(destination="tebra")
    _bump_destination_version(monkeypatch, "tebra", "2027.1")
    with pytest.raises(PipelineError) as caught:
        run_migration(
            MigrationCommand(
                export_dir=_csv_export_dir(tmp_path),
                out_dir=tmp_path / "out",
                source="clinic_csv",
                destination="tebra",
            )
        )
    assert caught.value.kind == "destination_mismatch"
    assert "has changed since" in str(caught.value)


def test_the_cli_refuses_before_it_draws_the_transit_map(tmp_path: Path) -> None:
    """No page of routes above a refusal for a move that cannot start."""
    from typer.testing import CliRunner

    from anastomosis.cli import app

    _teach_clinic_csv(destination="tebra")
    result = CliRunner().invoke(
        app,
        [
            "migrate",
            str(_csv_export_dir(tmp_path)),
            "--from",
            "clinic_csv",
            "--to",
            "epic",
            "-o",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 2
    assert "was taught for destination" in result.output
    assert "Anastomosis would use" not in result.output
    assert "Ways to file charts into" not in result.output


def test_teaching_against_an_unknown_destination_refuses_before_analysis() -> None:
    result = run_source_init_command(
        SourceInitCommand(
            example=CSV_FIXTURE, name="clinic_csv", confirmed=True, destination="no_such_ehr"
        )
    )
    assert result.ok is False
    assert result.error == "UnknownDestination"
    assert result.fmt_type is None  # the example was never even analysed


def test_an_unbound_mapping_still_runs_anywhere(tmp_path: Path, fake_chromium: None) -> None:
    """Teaching without --to is unchanged behavior: no binding, no refusal."""
    pytest.importorskip("pymupdf", reason="needs PyMuPDF")
    _teach_clinic_csv()
    result = run_migration(
        MigrationCommand(
            export_dir=_csv_export_dir(tmp_path),
            out_dir=tmp_path / "out",
            source="clinic_csv",
            destination="epic",
            force=True,
        )
    )
    assert result.transit.destination == "epic"


# --- explicit state transitions ------------------------------------------------


def _prepare_folder(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    write_run_manifest(
        out,
        _manifest_for(
            tmp_path,
            capture_binding(
                source="pf-tebra",
                destination="tebra",
                render_mode="neutral",
                pack="generic_soap",
            ),
        ),
    )
    return out


def test_prepared_advances_to_delivered_then_verified(tmp_path: Path) -> None:
    out = _prepare_folder(tmp_path)
    delivered = advance_state(out, RunState.DELIVERED, receipt="upload_report.json")
    assert delivered.state is RunState.DELIVERED
    assert delivered.receipt == "upload_report.json"
    verified = advance_state(out, RunState.VERIFIED, receipt="upload_report.json")
    assert verified.state_history == (RunState.PREPARED, RunState.DELIVERED, RunState.VERIFIED)
    # The binding itself is untouched by a state move.
    assert verified.binding_hash == delivered.binding_hash


@pytest.mark.parametrize(
    ("reach", "then"),
    [(RunState.PREPARED, RunState.VERIFIED), (RunState.DELIVERED, RunState.PREPARED)],
)
def test_illegal_transitions_refuse(tmp_path: Path, reach: RunState, then: RunState) -> None:
    out = _prepare_folder(tmp_path)
    if reach is RunState.DELIVERED:
        advance_state(out, RunState.DELIVERED, receipt="r")
    with pytest.raises(RunStateError, match=then.value):
        advance_state(out, then, receipt="r")


def test_an_unbound_folder_cannot_record_a_state(tmp_path: Path) -> None:
    with pytest.raises(BindingError, match="no run manifest"):
        advance_state(tmp_path, RunState.DELIVERED, receipt="r")


def test_a_drifted_folder_cannot_record_a_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _prepare_folder(tmp_path)
    _bump_destination_version(monkeypatch, "tebra", "2027.1")
    with pytest.raises(BindingError, match="destination profile changed"):
        advance_state(out, RunState.DELIVERED, receipt="r")
    assert read_run_manifest(out).state is RunState.PREPARED  # nothing recorded


def test_verify_binding_names_every_drifted_profile(tmp_path: Path) -> None:
    out = _prepare_folder(tmp_path)
    manifest = read_run_manifest(out)
    wrong = RunBinding(
        source=SourceProfile(name="other", kind=SOURCE_BUILTIN),
        destination=DestinationProfile(name="d", display="D", version="9", capabilities=()),
        layout=LayoutProfile(render_mode="neutral", pack="other"),
    )
    with pytest.raises(BindingError) as caught:
        verify_binding(manifest, wrong)
    assert {drift.profile for drift in caught.value.drifted} == {
        "source",
        "destination",
        "layout",
    }
    assert verify_binding(manifest, recapture_binding(manifest)) is None


# --- the upload side refuses the same way -------------------------------------


def test_upload_binding_check_refuses_a_drifted_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anastomosis.core.upload_command import check_run_binding

    out = _prepare_folder(tmp_path)
    assert check_run_binding(out) is not None  # current: uploads proceed
    _bump_destination_version(monkeypatch, "tebra", "2027.1")
    with pytest.raises(BindingError, match="destination profile changed"):
        check_run_binding(out)


def test_upload_over_an_unbound_tree_proceeds_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An output tree from before run manifests uploads exactly as it did — loudly."""
    from anastomosis.core.upload_command import check_run_binding

    with caplog.at_level("WARNING"):
        assert check_run_binding(tmp_path) is None
    assert "not bound to a set of profiles" in caplog.text


# --- PHI ----------------------------------------------------------------------


def test_the_run_manifest_carries_no_patient_value(tmp_path: Path, fake_chromium: None) -> None:
    """Hashes, names, versions and operator-chosen paths — nothing from the export."""
    pytest.importorskip("pymupdf", reason="needs PyMuPDF")
    _teach_clinic_csv(destination="tebra")
    out = tmp_path / "out"
    run_migration(
        MigrationCommand(
            export_dir=_csv_export_dir(tmp_path),
            out_dir=out,
            source="clinic_csv",
            destination="tebra",
            force=True,
        )
    )
    text = (out / RUN_MANIFEST_NAME).read_text(encoding="utf-8")
    for leak in (
        "Ada",
        "Fixture",
        "Boris",
        "900-12-3456",
        "ada@example.com",
        "555-0101",
        "Acute bronchitis",
        "1985",
    ):
        assert leak not in text, leak


def test_resolve_pack_is_the_one_render_mode_to_pack_answer() -> None:
    assert resolve_pack("neutral") == "generic_soap"
    assert resolve_pack("ccda-standard") is None
    assert resolve_pack("practice_fusion_soap") == "practice_fusion_soap"


# --- what the adversarial review found -----------------------------------------


def test_an_external_pack_survives_the_step_that_never_saw_pack_dir(
    tmp_path: Path, fake_chromium: None
) -> None:
    """The review's blocker: a `--pack-dir` migration could never be uploaded.

    `migrate` profiles the layout with the operator's `--pack-dir` list; the
    upload that follows has no such list, so re-running discovery there found
    nothing, recorded no hash, and refused — telling the operator to restore
    inputs that were never touched. The manifest records where the render read
    from, and the later step asks its question there.
    """
    pytest.importorskip("pymupdf", reason="needs PyMuPDF")
    from anastomosis.core.upload_command import check_run_binding

    vendor = tmp_path / "vendor"
    vendor.mkdir()
    shutil.copytree(
        Path(__file__).resolve().parents[2] / "src" / "anastomosis" / "packs" / "generic_soap",
        vendor / "vendor_soap",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    manifest_yaml = vendor / "vendor_soap" / "pack.yaml"
    manifest_yaml.write_text(
        manifest_yaml.read_text(encoding="utf-8").replace(
            "name: generic_soap", "name: vendor_soap"
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    run_migration(
        MigrationCommand(
            export_dir=PF_FIXTURE,
            out_dir=out,
            source="pf-tebra",
            destination="tebra",
            render="vendor_soap",
            pack_dirs=(vendor,),
            trust_new=True,
        )
    )
    bound = read_run_manifest(out)
    assert bound.binding.layout.content_hash is not None
    assert bound.binding.layout.root == str(vendor / "vendor_soap")

    # The upload knows nothing about --pack-dir, and must still be able to ask.
    assert check_run_binding(out) is not None

    # And it is still a real check: editing those bytes refuses.
    template = vendor / "vendor_soap" / "template.html"
    template.write_text(template.read_text(encoding="utf-8") + "<!-- edited -->", encoding="utf-8")
    with pytest.raises(BindingError) as caught:
        check_run_binding(out)
    assert [drift.profile for drift in caught.value.drifted] == ["layout"]


def test_a_pack_that_moved_but_did_not_change_is_not_drift(tmp_path: Path) -> None:
    """`root` is recorded and deliberately not hashed: same bytes, new place."""
    from anastomosis.core.profiles import reprofile_layout
    from anastomosis.reconstruct.packtrust import pack_content_hash

    first = tmp_path / "a"
    second = tmp_path / "b"
    shutil.copytree(
        Path(__file__).resolve().parents[2] / "src" / "anastomosis" / "packs" / "generic_soap",
        first / "generic_soap",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(first, second)
    here = LayoutProfile(
        render_mode="generic_soap",
        pack="generic_soap",
        origin="pack-dir",
        content_hash=pack_content_hash(first / "generic_soap"),
        root=str(first / "generic_soap"),
    )
    there = LayoutProfile(**{**here.__dict__, "root": str(second / "generic_soap")})
    assert here.profile_hash == there.profile_hash
    assert reprofile_layout(there).content_hash == here.content_hash


def test_the_upload_folder_below_a_bound_run_is_still_checked(
    tmp_path: Path, fake_chromium: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`anast upload -o <out>/charts` is a documented habit for the upload
    manifest, and it used to walk straight past the binding check: no run
    manifest in that folder, one PHI-free "not bound" line, every chart filed
    unchecked. The run manifest is one level up and is found there."""
    pytest.importorskip("pymupdf", reason="needs PyMuPDF")
    from anastomosis.core.upload_command import check_run_binding

    out = tmp_path / "out"
    run_migration(
        MigrationCommand(export_dir=PF_FIXTURE, out_dir=out, source="pf-tebra", destination="tebra")
    )
    assert check_run_binding(out / "charts") is not None

    _bump_destination_version(monkeypatch, "tebra", "2027.1")
    with pytest.raises(BindingError):
        check_run_binding(out / "charts")


def test_a_hand_edited_manifest_does_not_agree_with_itself(
    tmp_path: Path, fake_chromium: None
) -> None:
    """The recorded hashes are read back, so the file's own numbers have to
    match its own contents. Without that they were decoration: an editor who
    changed a content hash and left them alone would have been believed."""
    pytest.importorskip("pymupdf", reason="needs PyMuPDF")
    out = tmp_path / "out"
    run_migration(
        MigrationCommand(export_dir=PF_FIXTURE, out_dir=out, source="pf-tebra", destination="tebra")
    )
    path = out / RUN_MANIFEST_NAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["profiles"]["layout"]["content_hash"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RunManifestError, match="does not agree with itself"):
        read_run_manifest(out)


def test_the_manifest_never_writes_the_export_path(tmp_path: Path, fake_chromium: None) -> None:
    """A practice that drops one folder per patient names those folders after
    patients. The digest answers "the same export?" without saying which."""
    pytest.importorskip("pymupdf", reason="needs PyMuPDF")
    export = tmp_path / "MRN-00998877_Cora_Q_Specimen_1978-03-04"
    shutil.copytree(PF_FIXTURE, export)
    out = tmp_path / "out"
    run_migration(
        MigrationCommand(export_dir=export, out_dir=out, source="pf-tebra", destination="tebra")
    )
    text = (out / RUN_MANIFEST_NAME).read_text(encoding="utf-8")
    for stated in ("MRN-00998877", "Cora", "Specimen", "1978-03-04"):
        assert stated not in text, f"the manifest wrote {stated!r}"
    assert read_run_manifest(out).export_dir_id == export_dir_id(export)


def test_a_second_upload_still_reaches_verified(tmp_path: Path, fake_chromium: None) -> None:
    """A folder already at `delivered` — a `--no-verify` upload followed by a
    full one — used to make the first transition raise inside a shared `try`,
    swallowing it and stranding the run one state short of the truth forever."""
    pytest.importorskip("pymupdf", reason="needs PyMuPDF")
    from anastomosis.core.upload_command import record_upload_state

    out = tmp_path / "out"
    run_migration(
        MigrationCommand(export_dir=PF_FIXTURE, out_dir=out, source="pf-tebra", destination="tebra")
    )
    report = out / "upload_report.json"
    report.write_text("{}", encoding="utf-8")

    class _Clean:
        is_clean = True
        report_path = report

    record_upload_state(out, _Clean(), verified=False)  # type: ignore[arg-type]
    assert read_run_manifest(out).state is RunState.DELIVERED
    record_upload_state(out, _Clean(), verified=True)  # type: ignore[arg-type]
    assert read_run_manifest(out).state is RunState.VERIFIED
