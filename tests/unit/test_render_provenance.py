"""A folder of charts has to be able to name the bytes that produced
it. ``render_settings.json`` records which layout was NAMED, but
cannot see its contents: an edited ``template.html`` between two runs
leaves a clean second run saying the folder is up to date, with
nothing on disk to tell the difference — the content-hash trust gate
does not close it either, since an asset sits outside the hash
entirely.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import anastomosis.reconstruct.chromium as chromium
import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
from anastomosis.pipeline import PipelineError, run_pipeline
from anastomosis.reconstruct.packs import ORIGIN_BUILTIN, ORIGIN_PACK_DIR
from anastomosis.reconstruct.packtrust import pack_content_hash
from anastomosis.reconstruct.provenance import (
    RENDER_PROVENANCE_NAME,
    UNREADABLE,
    RenderProvenance,
    pack_file_digests,
    provenance_difference,
    swapped_templates,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"
BUILTIN = Path(__file__).resolve().parents[2] / "src" / "anastomosis" / "packs" / "generic_soap"


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


@pytest.fixture
def external_pack(tmp_path: Path) -> Path:
    """A copy of the shipped neutral layout, in a directory a run can be
    pointed at — so a test can edit it without touching the installed one."""
    pack = tmp_path / "packs" / "my_layout"
    shutil.copytree(BUILTIN, pack)
    return pack


def _run(
    out: Path,
    *,
    pack: str = "generic_soap",
    pack_dirs: list[Path] | None = None,
    force: bool = False,
) -> Any:
    return run_pipeline(
        export_dir=FIXTURE,
        out=out,
        source="pf-tebra",
        pack=pack,
        pack_dirs=pack_dirs,
        force=force,
        section=None,
        qa=False,
        trust_new=True,
    )


def _record(out: Path) -> dict[str, Any]:
    data = json.loads((out / RENDER_PROVENANCE_NAME).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


# --- what a run records ------------------------------------------------------


def test_a_run_names_the_bytes_its_charts_came_from(rendered: None, tmp_path: Path) -> None:
    out = tmp_path / "out"
    _run(out)

    record = _record(out)
    assert record["pack"] == "generic_soap"
    assert record["origin"] == ORIGIN_BUILTIN
    # The SAME number the trust gate checks, so a report can be read against the
    # trust store without re-deriving anything.
    assert record["content_hash"] == pack_content_hash(BUILTIN)
    # Every file in the layout, not only the three the trust hash covers.
    assert set(record["files"]) >= {"pack.yaml", "template.html", "context.py"}


def test_the_record_says_which_templates_the_render_actually_read(
    rendered: None, tmp_path: Path
) -> None:
    """Not "what was lying next to the charts" — what reached the compiler."""
    out = tmp_path / "out"
    _run(out)

    record = _record(out)
    assert record["templates"] == {"template.html": record["files"]["template.html"]}
    # context.py is measured but never READ by the renderer: it is executed.
    assert "context.py" not in record["templates"]


def test_an_external_layout_records_where_it_came_from(
    rendered: None, tmp_path: Path, external_pack: Path
) -> None:
    out = tmp_path / "out"
    _run(out, pack_dirs=[external_pack.parent])

    record = _record(out)
    assert record["origin"] == ORIGIN_PACK_DIR
    assert record["content_hash"] == pack_content_hash(external_pack)


def test_the_record_is_deterministic(rendered: None, tmp_path: Path) -> None:
    """Two runs over one layout write byte-identical records: no clock, no set
    iteration order, nothing a diff would have to be taught to ignore."""
    first, second = tmp_path / "a", tmp_path / "b"
    _run(first)
    _run(second)

    assert (first / RENDER_PROVENANCE_NAME).read_bytes() == (
        second / RENDER_PROVENANCE_NAME
    ).read_bytes()


# --- the re-run guard ---------------------------------------------------------


def test_an_unchanged_layout_still_skips(rendered: None, tmp_path: Path) -> None:
    """The idempotent skip is the point of the directory; it must survive."""
    out = tmp_path / "out"
    first = _run(out)
    second = _run(out)

    assert len(first.render_result.rendered) == 6
    assert len(second.render_result.skipped) == 6


def test_an_edited_template_refuses_the_re_run(
    rendered: None, tmp_path: Path, external_pack: Path
) -> None:
    out = tmp_path / "out"
    _run(out, pack_dirs=[external_pack.parent])
    template = external_pack / "template.html"
    template.write_text(
        template.read_text(encoding="utf-8").replace("</body>", "<p>after review</p></body>"),
        encoding="utf-8",
    )

    with pytest.raises(PipelineError) as caught:
        _run(out, pack_dirs=[external_pack.parent])

    assert caught.value.exit_code == 2
    assert caught.value.kind == "layout_changed"
    # Names the file, so the review has somewhere to start.
    assert "template.html changed" in str(caught.value)
    assert "--force" in str(caught.value)


def test_an_edited_asset_refuses_too_though_the_trust_hash_cannot_see_it(
    rendered: None, tmp_path: Path, external_pack: Path
) -> None:
    """The hole the trust gate leaves open: assets are outside the content hash,
    so an edited logo re-runs clean and re-trusts clean. It still changes what
    every chart looks like."""
    asset = external_pack / "assets" / "mark.svg"
    asset.parent.mkdir()
    asset.write_text("<svg/>", encoding="utf-8")
    out = tmp_path / "out"
    _run(out, pack_dirs=[external_pack.parent])
    trusted_hash = pack_content_hash(external_pack)

    asset.write_text("<svg><rect/></svg>", encoding="utf-8")

    assert pack_content_hash(external_pack) == trusted_hash, "the trust gate sees nothing"
    with pytest.raises(PipelineError) as caught:
        _run(out, pack_dirs=[external_pack.parent])
    assert "assets/mark.svg changed" in str(caught.value)


def test_a_removed_asset_refuses_and_says_it_is_gone(
    rendered: None, tmp_path: Path, external_pack: Path
) -> None:
    asset = external_pack / "assets" / "mark.svg"
    asset.parent.mkdir()
    asset.write_text("<svg/>", encoding="utf-8")
    out = tmp_path / "out"
    _run(out, pack_dirs=[external_pack.parent])

    asset.unlink()

    with pytest.raises(PipelineError) as caught:
        _run(out, pack_dirs=[external_pack.parent])
    assert "assets/mark.svg removed" in str(caught.value)


def test_force_rebuilds_from_the_new_bytes_and_re_records_them(
    rendered: None, tmp_path: Path, external_pack: Path
) -> None:
    out = tmp_path / "out"
    _run(out, pack_dirs=[external_pack.parent])
    template = external_pack / "template.html"
    template.write_text(
        template.read_text(encoding="utf-8").replace("</body>", "<p>after review</p></body>"),
        encoding="utf-8",
    )

    result = _run(out, pack_dirs=[external_pack.parent], force=True)

    assert len(result.render_result.rendered) == 6
    assert (
        _record(out)["files"]["template.html"] == pack_file_digests(external_pack)["template.html"]
    )


def test_a_folder_with_no_record_is_not_refused(rendered: None, tmp_path: Path) -> None:
    """A directory an older build filled has no provenance in it. A guard that
    refused those would only ever punish upgrading."""
    out = tmp_path / "out"
    _run(out)
    (out / RENDER_PROVENANCE_NAME).unlink()

    result = _run(out)

    assert len(result.render_result.skipped) == 6


def test_an_unreadable_record_is_not_refused(rendered: None, tmp_path: Path) -> None:
    out = tmp_path / "out"
    _run(out)
    (out / RENDER_PROVENANCE_NAME).write_text("{not json", encoding="utf-8")

    result = _run(out)

    assert len(result.render_result.skipped) == 6


# --- the pieces ---------------------------------------------------------------


def test_pack_file_digests_leaves_out_the_bytecode_cache(tmp_path: Path) -> None:
    """``__pycache__`` is written BY loading the pack, so including it would
    make every second run disagree with the first for no layout reason."""
    pack = tmp_path / "pack"
    (pack / "__pycache__").mkdir(parents=True)
    (pack / "__pycache__" / "context.cpython-312.pyc").write_bytes(b"\x00")
    (pack / "context.py").write_text("x = 1\n", encoding="utf-8")

    assert sorted(pack_file_digests(pack)) == ["context.py"]


def test_a_directory_where_a_file_is_expected_is_not_measured(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "template.html").write_text("<html></html>", encoding="utf-8")
    (pack / "locked").mkdir()  # a directory where a file name is expected

    digests = pack_file_digests(pack)

    assert "locked" not in digests  # a directory is not a file
    assert set(digests) == {"template.html"}


def test_an_unreadable_pack_file_records_the_unreadable_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RULES.md 26: a file inside the pack that cannot be read is RECORDED as
    unreadable, never dropped and never raised — a record that omitted it would
    compare equal to a run where the file was fine. The read failure is
    injected: this suite can run as a uid no permission bit stops."""
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "template.html").write_text("<html></html>", encoding="utf-8")
    (pack / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    real_open = Path.open

    def _refuse(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == "logo.png":
            raise PermissionError(self.name)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _refuse)
    digests = pack_file_digests(pack)

    assert digests["logo.png"] == UNREADABLE
    assert len(digests["template.html"]) == 64
    assert digests["template.html"] != UNREADABLE


def test_a_template_swapped_mid_batch_is_named() -> None:
    """Some charts from one layout and some from another is the one thing no
    single record can honestly describe."""
    provenance = RenderProvenance(
        pack="demo",
        origin=ORIGIN_PACK_DIR,
        content_hash="abc",
        files={"template.html": "aaa", "partials/head.html": "bbb"},
        templates={"template.html": "ZZZ", "partials/head.html": "bbb"},
    )

    assert swapped_templates(provenance) == ["template.html"]


@pytest.mark.parametrize(
    ("previous_files", "current_files", "expected"),
    [
        ({"a": "1"}, {"a": "2"}, "a changed"),
        ({"a": "1"}, {"a": "1", "b": "2"}, "b added"),
        ({"a": "1", "b": "2"}, {"a": "1"}, "b removed"),
    ],
)
def test_provenance_difference_names_the_change(
    previous_files: dict[str, str], current_files: dict[str, str], expected: str
) -> None:
    base: dict[str, Any] = {"version": 1, "pack": "demo", "origin": "builtin", "content_hash": "h"}

    difference = provenance_difference(
        {**base, "files": previous_files}, {**base, "files": current_files}
    )

    assert difference == expected


def test_provenance_difference_is_silent_about_a_version_it_cannot_read() -> None:
    """A record from another build is not evidence of another layout."""
    previous = {"version": 99, "pack": "demo", "files": {"a": "1"}}
    current = {"version": 1, "pack": "demo", "origin": "builtin", "files": {"a": "2"}}

    assert provenance_difference(previous, current) == ""


def test_the_difference_stops_naming_files_and_starts_counting() -> None:
    """A layout rewritten wholesale must not print a hundred filenames at an
    operator who needs one sentence."""
    base: dict[str, Any] = {"version": 1, "pack": "demo", "origin": "builtin", "content_hash": "h"}
    previous = {**base, "files": {f"f{i}": "1" for i in range(9)}}
    current = {**base, "files": {f"f{i}": "2" for i in range(9)}}

    difference = provenance_difference(previous, current)

    assert difference.endswith("and 4 more")


def test_a_crlf_template_agrees_with_its_own_bytes(tmp_path: Path) -> None:
    """The digest the loader records is the digest of the file on
    disk: Jinja opens templates in text mode, so universal newline
    translation turns a CRLF file into LF before the loader sees the
    string, which would otherwise disagree with the binary digest for
    every CRLF template."""
    import hashlib

    import jinja2

    from anastomosis.reconstruct.provenance import RecordingLoader

    crlf = tmp_path / "template.html"
    crlf.write_bytes(b"<html>\r\n<body>{{ patient }}</body>\r\n</html>\r\n")
    loader = RecordingLoader(tmp_path)
    jinja2.Environment(loader=loader, autoescape=True).get_template("template.html")

    assert loader.templates_read["template.html"] == hashlib.sha256(crlf.read_bytes()).hexdigest()
    assert (
        swapped_templates(
            RenderProvenance(
                pack="p",
                origin="pack-dir",
                content_hash="x",
                files=dict(loader.templates_read),
                templates=dict(loader.templates_read),
            )
        )
        == []
    )


def test_an_asset_behind_a_symlinked_directory_is_still_measured(tmp_path: Path) -> None:
    """`rglob` does not follow a symlinked directory: it calls the link a file,
    and the is-file guard then drops it, so a pack whose `assets` is a link had
    its whole subtree missing from the record — and editing the logo through it
    changed nothing here. That is the exact claim this record exists to make.
    """
    import os

    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "pack.yaml").write_text("name: p\n", encoding="utf-8")
    (pack / "context.py").write_text("", encoding="utf-8")
    (pack / "template.html").write_text("<html></html>", encoding="utf-8")
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "mark.svg").write_text("<svg/>", encoding="utf-8")
    os.symlink(real, pack / "assets")

    before = pack_file_digests(pack)
    assert "assets/mark.svg" in before

    (real / "mark.svg").write_text("<svg>EDITED</svg>", encoding="utf-8")
    assert pack_file_digests(pack) != before
