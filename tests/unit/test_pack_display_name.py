"""The name a person typed, read back where a person reads it.

``anast pack init --display "Acme SOAP note"`` and ``anast source init
--display`` both asked an author what their thing should be called. The answer
was interpolated into a sentence in the generated description and never
recoverable as a name again, so every surface that lists a layout or a format
had to invent one by re-casing the id — and no re-casing of ``ccda`` produces
"C-CDA", which is why the front end carried that one as a hard-coded exception.

These pin the round trip end to end: typed at the command, written as a field,
loaded by the manifest, carried through the info surface, and different from
the id. Plus the property that made it worth doing carefully — a registration
without one still shows something.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from anastomosis.core.commands import get_toolkit_info
from anastomosis.reconstruct.packs import PackManifest


def test_a_pack_can_declare_the_name_the_author_typed(tmp_path: Path) -> None:
    """`anast pack init --display "Acme SOAP note"` reaches pack.yaml as a field.

    It reached the file before this — inside the description sentence, where
    nothing could read it back.
    """
    pytest.importorskip("pymupdf", reason="the emitter reads sample geometry")
    from test_packgen_emit import _make_samples  # type: ignore[import-not-found]

    from anastomosis.packgen.emit import emit_draft_pack
    from anastomosis.packgen.extract import extract_samples
    from anastomosis.packgen.infer import analyze

    samples = tmp_path / "samples"
    samples.mkdir()
    analysis = analyze(extract_samples(_make_samples(samples)))

    # A colon and a quote, because this is free text an author typed and YAML
    # cares about both: unquoted, `Acme: the "SOAP" note` is a mapping inside a
    # mapping and the file will not load at all. The emitter already treats
    # inferred headings this way; a display name is no different.
    typed = 'Acme: the "SOAP" note'
    pack_dir = emit_draft_pack(analysis, name="acme_soap", display=typed, out_dir=tmp_path)
    data = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))

    assert data["display"] == typed
    manifest = PackManifest.model_validate(data)
    assert manifest.display == typed


def test_a_pack_without_a_display_name_is_still_valid(tmp_path: Path) -> None:
    """`extra="forbid"` means the field had to be optional or every pack broke."""
    manifest = PackManifest.model_validate({"name": "third_party", "version": "1.0"})

    assert manifest.display == ""


def test_the_built_in_layouts_carry_their_own_names() -> None:
    by_name = {p.name: p.display for p in get_toolkit_info().packs}

    assert by_name["generic_soap"] == "Generic SOAP"
    assert by_name["practice_fusion_soap"] == "Practice Fusion SOAP"


def test_every_source_carries_a_name_no_recasing_could_produce() -> None:
    """`ccda` is the case that proves it: the front end had it hard-coded."""
    by_name = {name: display for name, display, _desc in get_toolkit_info().sources}

    # Scoped to the adapters this repository ships. The registry is a global
    # that any test (or any third party) can add to, so asserting a property of
    # everything in it at some moment is asserting something about whoever ran
    # first.
    shipped = {"ccda", "fhir-r4", "oracle-ehi", "pf-tebra"}
    assert shipped <= set(by_name), by_name

    assert by_name["ccda"] == "C-CDA"
    assert by_name["pf-tebra"] == "Practice Fusion / Tebra"
    for name in shipped:
        assert by_name[name] and by_name[name] != name, f"{name} still reads as its id"


def test_a_registration_with_no_name_reads_as_its_id() -> None:
    """The registry is open; an adapter this repo never type-checked can join it.

    It reads as its own id — which is what every adapter read as before — rather
    than as an empty string or a crash.
    """
    import anastomosis.sources.base as base

    class _Nameless:
        name = "third-party"
        description = "an adapter written before display existed"

        def detect(self, path: Path) -> bool:
            return False

        def load(self, path: Path):  # type: ignore[no-untyped-def]
            return iter(())

    registry = dict(base._REGISTRY)
    registry["third-party"] = _Nameless()  # type: ignore[assignment]
    original, base._REGISTRY = base._REGISTRY, registry
    try:
        by_name = {name: display for name, display, _desc in get_toolkit_info().sources}
    finally:
        base._REGISTRY = original

    assert by_name["third-party"] == "third-party"
