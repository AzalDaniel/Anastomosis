"""Tests for template-pack discovery: the defensive-loading invariant."""

from pathlib import Path

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
    make_pack(tmp_path, "broken", manifest="name: [unclosed")
    make_pack(tmp_path, "fine")
    statuses = discover_packs([tmp_path], allow_external=True)
    assert statuses["fine"].available
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


def test_first_definition_wins_user_shadows_builtin(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    make_pack(a)
    make_pack(b, manifest=GOOD_MANIFEST.replace('"1.0"', '"2.0"'))
    statuses = discover_packs([a, b], allow_external=True)
    pack = statuses["demo_soap"].pack
    assert pack is not None and pack.manifest.version == "1.0"
