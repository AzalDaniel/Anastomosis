"""Tests for template-pack discovery: the defensive-loading invariant."""

from pathlib import Path

from anastomosis.core.packdirs import (
    ORIGIN_BUILTIN,
    ORIGIN_PACK_DIR,
    ORIGIN_USER,
    candidate_pack_dirs,
)
from anastomosis.reconstruct import discover_packs

GOOD_MANIFEST = """\
name: demo_soap
version: "1.0"
description: demo pack
timezone: America/Chicago
sections:
  addenda: {label: "Addenda", default: true}
  insurance: {label: "Insurance", default: false, description: "payment info"}
tokens:
  heading_fill: "#f1f1f1"
verify_header_fields: [patient_name, dob]
"""

GOOD_CONTEXT = """\
def build_context(encounter, record, cfg):
    return {"patient": record, "encounter": encounter, "cfg": cfg}
"""


def make_pack(
    root: Path,
    name: str = "demo_soap",
    *,
    manifest: str = GOOD_MANIFEST,
    context: str = GOOD_CONTEXT,
    template: str | None = "<html>{{ patient }}</html>",
) -> Path:
    pack = root / name
    pack.mkdir(parents=True)
    (pack / "pack.yaml").write_text(manifest)
    (pack / "context.py").write_text(context)
    if template is not None:
        (pack / "template.html").write_text(template)
    return pack


def test_good_pack_loads_with_sections_and_tokens(tmp_path: Path) -> None:
    make_pack(tmp_path)
    statuses = discover_packs([tmp_path], allow_external=True)
    status = statuses["demo_soap"]
    assert status.available and status.pack is not None
    manifest = status.pack.manifest
    assert manifest.timezone == "America/Chicago"
    assert manifest.sections["insurance"].default is False
    assert manifest.tokens["heading_fill"] == "#f1f1f1"
    assert status.pack.build_context(None, None, None)["cfg"] is None


def test_pack_dir_may_be_a_single_pack(tmp_path: Path) -> None:
    pack = make_pack(tmp_path)
    statuses = discover_packs([pack], allow_external=True)
    assert statuses["demo_soap"].available


def test_broken_manifest_is_diagnosed_not_fatal(tmp_path: Path) -> None:
    # An unparseable manifest is keyed by its directory name (the manifest
    # name is unreadable); the healthy sibling still loads.
    make_pack(tmp_path, "broken", manifest="name: [unclosed")
    make_pack(tmp_path, "fine")
    statuses = discover_packs([tmp_path], allow_external=True)
    assert statuses["demo_soap"].available
    broken = statuses["broken"]
    assert not broken.available
    assert broken.diagnosis is not None and "Error" in broken.diagnosis


def test_missing_template_is_diagnosed(tmp_path: Path) -> None:
    make_pack(tmp_path, template=None)
    status = discover_packs([tmp_path], allow_external=True)["demo_soap"]
    assert not status.available
    assert status.diagnosis is not None and "template.html" in status.diagnosis


def test_crashing_context_is_diagnosed(tmp_path: Path) -> None:
    make_pack(tmp_path, context="raise RuntimeError('vendor changed everything')")
    status = discover_packs([tmp_path], allow_external=True)["demo_soap"]
    assert not status.available
    assert status.diagnosis is not None and "RuntimeError" in status.diagnosis


def test_context_without_builder_is_diagnosed(tmp_path: Path) -> None:
    make_pack(tmp_path, context="x = 1")
    status = discover_packs([tmp_path], allow_external=True)["demo_soap"]
    assert not status.available
    assert status.diagnosis is not None and "build_context" in status.diagnosis


def test_external_packs_require_opt_in(tmp_path: Path) -> None:
    make_pack(tmp_path)
    status = discover_packs([tmp_path])["demo_soap"]
    assert not status.available
    assert status.diagnosis is not None and "external" in status.diagnosis


def test_unknown_manifest_keys_are_rejected(tmp_path: Path) -> None:
    # extra=forbid: a typo'd manifest key is a diagnosis, not silent drift.
    make_pack(tmp_path, manifest=GOOD_MANIFEST + "page_color: red\n")
    status = discover_packs([tmp_path], allow_external=True)["demo_soap"]
    assert not status.available


def test_collision_field_accepts_its_only_implemented_value(tmp_path: Path) -> None:
    # "guid_suffix" is the one behavior _allocate_target hardcodes; declaring it
    # explicitly must load exactly like the (identical) default.
    make_pack(tmp_path, manifest=GOOD_MANIFEST + "filename:\n  collision: guid_suffix\n")
    status = discover_packs([tmp_path], allow_external=True)["demo_soap"]
    assert status.available and status.pack is not None
    assert status.pack.manifest.filename.collision == "guid_suffix"


def test_collision_field_refuses_any_other_value(tmp_path: Path) -> None:
    # collision is parsed but never read by the render path — a value other
    # than the one hardcoded behavior implements must be a loud refusal, not a
    # silently-ignored config (PHI-adjacent: a healthcare tool must not pretend
    # to honor a same-day-visit collision policy it does not run).
    make_pack(tmp_path, manifest=GOOD_MANIFEST + "filename:\n  collision: overwrite\n")
    status = discover_packs([tmp_path], allow_external=True)["demo_soap"]
    assert not status.available
    assert status.diagnosis is not None
    assert "collision" in status.diagnosis


def test_first_definition_wins_user_shadows_builtin(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    make_pack(a)
    make_pack(b, manifest=GOOD_MANIFEST.replace('"1.0"', '"2.0"'))
    statuses = discover_packs([a, b], allow_external=True)
    pack = statuses["demo_soap"].pack
    assert pack is not None and pack.manifest.version == "1.0"


# --- the one walk both loaders take ------------------------------------------

ONE_PACK_EACH = [
    ("explicit/demo_soap", ORIGIN_PACK_DIR),
    ("user/demo_soap", ORIGIN_USER),
    ("builtin/demo_soap", ORIGIN_BUILTIN),
]


def three_origins(tmp_path: Path) -> dict[str, Path]:
    dirs = {kind: tmp_path / kind for kind in ("explicit", "user", "builtin")}
    for parent in dirs.values():
        make_pack(parent)
    return dirs


def walk(tmp_path: Path, dirs: dict[str, Path], name: str | None = None) -> list[tuple[str, str]]:
    found = candidate_pack_dirs(
        [dirs["explicit"]],
        user_dir=dirs["user"],
        builtin_dir=dirs["builtin"],
        manifest="pack.yaml",
        name=name,
    )
    return [(root.relative_to(tmp_path).as_posix(), origin) for root, origin in found]


def test_the_walk_offers_the_three_origins_in_rule_21_order(tmp_path: Path) -> None:
    assert walk(tmp_path, three_origins(tmp_path)) == ONE_PACK_EACH


def test_the_walk_narrowed_to_one_name_keeps_that_order(tmp_path: Path) -> None:
    dirs = three_origins(tmp_path)
    assert walk(tmp_path, dirs, "demo_soap") == ONE_PACK_EACH
    assert walk(tmp_path, dirs, "no_such_pack") == []


def test_a_parent_that_is_itself_a_pack_is_offered_whole(tmp_path: Path) -> None:
    """``anast doctor`` and ``--pack-dir`` may name one pack, not a shelf."""
    dirs = three_origins(tmp_path)
    inner = {**dirs, "explicit": dirs["explicit"] / "demo_soap"}
    assert walk(tmp_path, inner) == ONE_PACK_EACH
    assert walk(tmp_path, inner, "demo_soap") == ONE_PACK_EACH


def test_a_named_walk_looks_past_a_parent_pack_of_another_name(tmp_path: Path) -> None:
    """An unnamed walk takes such a parent whole; a walk asking for one
    name still reaches that parent's ``<name>/`` child."""
    dirs = three_origins(tmp_path)
    (dirs["explicit"] / "pack.yaml").write_text(GOOD_MANIFEST)
    assert walk(tmp_path, dirs)[0] == ("explicit", ORIGIN_PACK_DIR)
    assert walk(tmp_path, dirs, "demo_soap")[0] == ("explicit/demo_soap", ORIGIN_PACK_DIR)


def test_the_walk_skips_the_user_directory_when_asked(tmp_path: Path) -> None:
    """The install self-check asks only about the shipped layouts."""
    dirs = three_origins(tmp_path)
    found = candidate_pack_dirs(
        [dirs["explicit"]],
        user_dir=None,
        builtin_dir=dirs["builtin"],
        manifest="pack.yaml",
    )
    assert [origin for _root, origin in found] == [ORIGIN_PACK_DIR, ORIGIN_BUILTIN]


# --- coverage: what a layout carries out of the record ----------------------


def test_every_shipped_pack_places_every_kind_in_exactly_one_half() -> None:
    """A kind in neither ``carries`` nor ``omits`` is forgotten, not
    excused, and QA cannot tell a lost section from a layout that never
    had one — the reason coverage is two lists, not one. Adding a kind to
    ``CHARTABLE_KINDS`` must fail this until every shipped pack says
    something about it."""
    from anastomosis.core.model import CHARTABLE_KINDS

    for name, status in sorted(discover_packs().items()):
        assert status.pack is not None, status.diagnosis
        coverage = status.pack.manifest.coverage
        placed = set(coverage.carries) | set(coverage.omits)
        missing = sorted(set(CHARTABLE_KINDS) - placed)
        assert not missing, f"{name} says nothing about {missing}"


def test_a_pack_may_not_claim_a_kind_both_ways(tmp_path: Path) -> None:
    manifest = GOOD_MANIFEST + (
        "coverage:\n  carries: [conditions]\n  omits: {conditions: 'no problem list here'}\n"
    )
    make_pack(tmp_path, manifest=manifest)
    status = discover_packs([tmp_path], allow_external=True)["demo_soap"]
    assert status.pack is None
    assert status.diagnosis is not None and "carried and omitted" in status.diagnosis


def test_a_pack_may_not_omit_a_kind_without_saying_why(tmp_path: Path) -> None:
    """An empty reason is how an exemption outlives the reason for it."""
    manifest = GOOD_MANIFEST + "coverage:\n  omits: {conditions: ''}\n"
    make_pack(tmp_path, manifest=manifest)
    status = discover_packs([tmp_path], allow_external=True)["demo_soap"]
    assert status.pack is None
    assert status.diagnosis is not None and "need a reason" in status.diagnosis


def test_a_pack_may_not_invent_a_kind(tmp_path: Path) -> None:
    manifest = GOOD_MANIFEST + "coverage:\n  carries: [horoscopes]\n"
    make_pack(tmp_path, manifest=manifest)
    status = discover_packs([tmp_path], allow_external=True)["demo_soap"]
    assert status.pack is None
    assert status.diagnosis is not None and "unknown kind" in status.diagnosis
