import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

import anastomosis
import anastomosis.reconstruct.chromium as chromium
from anastomosis.cli import app

runner = CliRunner()

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert anastomosis.__version__ in result.output


def test_info_lists_sources_and_packs() -> None:
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert "anastomosis" in result.output
    assert "pf-tebra" in result.output
    assert "generic_soap" in result.output


class _FakeChromium:
    """Stands in for Chromium by writing a REAL pdf carrying the chart text,
    so the CLI's QA stage runs for real against what was 'rendered'."""

    def __init__(self, **kwargs: object) -> None:
        pass

    def render(self, html: str, pdf_path: Path) -> None:
        import fitz

        from anastomosis.core.textutil import html_to_text

        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_textbox(
            fitz.Rect(18, 18, 594, 774), html_to_text(html) or "(empty)", fontsize=7
        )
        doc.save(str(pdf_path))
        doc.close()

    def close(self) -> None:
        pass


def test_pipeline_run_end_to_end_with_qa(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fitz", reason="pipeline QA e2e needs PyMuPDF (render extra)")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    out = tmp_path / "charts"
    result = runner.invoke(
        app, ["pipeline", "run", str(FIXTURE), "--out", str(out), "--section", "insurance=on"]
    )
    assert result.exit_code == 0, result.output
    assert "Detected source" in result.output and "pf-tebra" in result.output
    assert "6 rendered" in result.output
    assert "QA: 6 pass" in result.output
    assert len(list(out.glob("*.pdf"))) == 6
    assert (out / "_PHI_WARNING_README.txt").exists()
    assert (out / "qa_report.json").exists()


def test_pipeline_run_delivery_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the CLI's archive/bundle/ccda summary lines (3 patients in the
    fixture), so a future change to the delivery output is caught."""
    pytest.importorskip("fitz", reason="delivery e2e needs PyMuPDF (render extra)")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    out = tmp_path / "charts"
    result = runner.invoke(
        app,
        [
            "pipeline",
            "run",
            str(FIXTURE),
            "--out",
            str(out),
            "--archive",
            str(tmp_path / "arc"),
            "--bundle",
            str(tmp_path / "bun"),
            "--ccda",
            str(tmp_path / "cda"),
        ],
    )
    assert result.exit_code == 0, result.output
    normalized = " ".join(result.output.split())
    assert "Archive: 3 patients, 6 encounters, 6 pdfs" in normalized
    assert "Bundles: 3 patients" in normalized
    assert "C-CDA: 3 patients" in normalized


def test_pipeline_run_rejects_unknown_dir(tmp_path: Path) -> None:
    result = runner.invoke(app, ["pipeline", "run", str(tmp_path), "--out", str(tmp_path / "o")])
    assert result.exit_code == 2
    assert "Could not identify" in result.output


def test_pipeline_run_diagnoses_unknown_pack(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["pipeline", "run", str(FIXTURE), "--out", str(tmp_path / "o"), "--pack", "nope"],
    )
    assert result.exit_code == 2
    assert "unavailable" in result.output


# --- operator-input boundary (clean exit 2, never a traceback) --------------


def test_pipeline_run_out_is_a_file_is_clean_exit_2(tmp_path: Path) -> None:
    """`--out` pointing at an existing FILE is a clean exit 2, not a
    FileExistsError traceback from secure_output_dir()."""
    not_a_dir = tmp_path / "not_a_dir"
    not_a_dir.write_text("x")
    result = runner.invoke(app, ["pipeline", "run", str(FIXTURE), "--out", str(not_a_dir)])
    assert result.exit_code == 2, result.output
    # Rich may wrap the long tmp path across lines, so normalize whitespace.
    assert "is a file, not a directory" in " ".join(result.output.split())
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_archive_out_is_a_file_is_clean_exit_2(tmp_path: Path) -> None:
    """`anast archive --out <file>` (which would create <file>/_charts) is a
    clean exit 2, not a FileExistsError traceback."""
    archive_file = tmp_path / "archive_file"
    archive_file.write_text("x")
    result = runner.invoke(app, ["archive", str(FIXTURE), "--out", str(archive_file)])
    assert result.exit_code == 2, result.output
    assert "is a file, not a directory" in " ".join(result.output.split())


def test_destination_list_bad_registry_is_clean_exit_2(tmp_path: Path) -> None:
    """A malformed `--registry` overlay is a clean exit 2, not a pydantic
    ValidationError traceback."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("entries: [not-a-mapping]\n")
    result = runner.invoke(app, ["destination", "list", "--registry", str(bad)])
    assert result.exit_code == 2, result.output
    assert "Invalid destination registry" in " ".join(result.output.split())
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_section_bad_value_is_clean_exit_2(tmp_path: Path) -> None:
    """`--section insurance=maybe` is rejected (exit 2) rather than silently
    coerced to off and quietly changing backend state."""
    result = runner.invoke(
        app,
        [
            "pipeline",
            "run",
            str(FIXTURE),
            "--out",
            str(tmp_path / "o"),
            "--section",
            "insurance=maybe",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "on/off" in " ".join(result.output.split())


def test_section_missing_value_is_clean_exit_2(tmp_path: Path) -> None:
    """A bare `--section insurance` (no =value) is rejected, not coerced to off."""
    result = runner.invoke(
        app,
        ["pipeline", "run", str(FIXTURE), "--out", str(tmp_path / "o"), "--section", "insurance"],
    )
    assert result.exit_code == 2, result.output
    assert "NAME=on" in " ".join(result.output.split())


def test_section_unknown_name_is_clean_exit_2(tmp_path: Path) -> None:
    """A typo'd / unknown section name is rejected against the pack's matrix
    (exit 2), not silently ignored."""
    result = runner.invoke(
        app,
        ["pipeline", "run", str(FIXTURE), "--out", str(tmp_path / "o"), "--section", "bogus=on"],
    )
    assert result.exit_code == 2, result.output
    normalized = " ".join(result.output.split())
    assert "Unknown --section" in normalized and "bogus" in normalized


def test_gui_without_pywebview_shows_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate the gui extra not being installed: `import webview` fails inside
    # the shell's launch(), which raises a RuntimeError naming anastomosis[gui];
    # the CLI must surface that as a clean exit, never a traceback.
    import builtins

    real_import = builtins.__import__

    def _no_webview(name: str, *args: object, **kwargs: object) -> object:
        if name == "webview":
            raise ImportError("No module named 'webview'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_webview)
    result = runner.invoke(app, ["gui"])
    assert result.exit_code == 1
    assert "anastomosis[gui]" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_destination_list_shows_tebra() -> None:
    result = runner.invoke(app, ["destination", "list"])
    assert result.exit_code == 0, result.output
    assert "tebra" in result.output
    assert "unverified" in result.output


def test_destination_route_tebra_routes_via_ccda_import() -> None:
    # The shipped registry: tebra has no write API (verified-none) but does
    # have in-product C-CDA import, so a route exists.
    result = runner.invoke(app, ["destination", "route", "tebra"])
    assert result.exit_code == 0, result.output
    assert "delivery routes for tebra" in result.output
    assert "chosen: ccda_import" in result.output


def test_destination_route_unroutable_exits_1_with_map(tmp_path: Path) -> None:
    # An all-unverified destination (via --registry overlay) has no viable
    # route; the operator must see that loudly (exit 1) with the full map.
    overlay = tmp_path / "registry.yaml"
    overlay.write_text(
        "entries:\n"
        "  nowhere:\n"
        "    name: nowhere\n"
        "    display: Nowhere EHR\n"
        "    doc_write_api: {kind: unverified}\n"
        "    ccda_import: {kind: unverified}\n"
        "    browser: {kind: none}\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["destination", "route", "nowhere", "--registry", str(overlay)])
    assert result.exit_code == 1, result.output
    assert "delivery routes for nowhere" in result.output
    assert "no viable route" in result.output


def test_destination_route_unknown_is_clean_error() -> None:
    result = runner.invoke(app, ["destination", "route", "ghost"])
    assert result.exit_code == 2
    assert "unknown destination" in result.output
    assert "ghost" in result.output
    # A clean error, not a traceback.
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_destination_list_shows_pack_column_needs_discovery() -> None:
    # The shipped tebra pack is present but undiscovered: the pack column says so.
    result = runner.invoke(app, ["destination", "list"])
    assert result.exit_code == 0, result.output
    assert "needs-discovery" in result.output


# --- anast destination init (the selector-discovery wizard) -----------------

import anastomosis.cli as cli  # noqa: E402
import anastomosis.destinations.loader as dest_loader  # noqa: E402
from anastomosis.destinations.browserpack import SelectorMap  # noqa: E402
from anastomosis.destinations.loader import load_destination_pack  # noqa: E402

# Eleven slots in canonical order: 9 required + 2 optional.
_ALL_SLOTS = (*SelectorMap.required_slots(), *SelectorMap.optional_slots())


def _good_selectors() -> dict[str, str]:
    return {slot: f"#{slot}" for slot in _ALL_SLOTS}


class _FakeValidator:
    """A seam-injected validator: ``found`` selectors match 1, all else 0."""

    def __init__(self, found: set[str]) -> None:
        self._found = found

    def count(self, selector: str) -> int:
        return 1 if selector in self._found else 0


def test_destination_init_writes_selectors_yaml(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    # One line of stdin per prompt, in slot order.
    answers = "\n".join(_good_selectors()[slot] for slot in _ALL_SLOTS) + "\n"
    result = runner.invoke(
        app,
        ["destination", "init", "tebra", "--out-dir", str(out_dir)],
        input=answers,
    )
    assert result.exit_code == 0, result.output
    written = out_dir / "tebra" / "selectors.yaml"
    assert written.is_file()
    if os.name == "posix":
        # The pack dir is operator-private (same hardening as output dirs).
        assert stat.S_IMODE((out_dir / "tebra").stat().st_mode) == 0o700
    text = written.read_text(encoding="utf-8")
    # Header records generation provenance + re-run instructions.
    assert "GENERATED" in text
    assert "anast destination init tebra" in text
    assert "pack: tebra" in text
    # Parse it back and confirm every required slot landed.
    import yaml

    parsed = yaml.safe_load(text)["selectors"]
    for slot in SelectorMap.required_slots():
        assert parsed[slot] == f"#{slot}"
    # The registry overlay snippet is printed (not applied to the packaged file).
    assert "kind: pack" in result.output
    assert "detail: destinations/tebra" in result.output
    # Without --cdp, the as-is warning is printed.
    assert "preflight validates" in result.output


def test_destination_init_loaded_pack_is_then_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = tmp_path / "out"
    answers = "\n".join(_good_selectors()[slot] for slot in _ALL_SLOTS) + "\n"
    runner.invoke(app, ["destination", "init", "tebra", "--out-dir", str(out_dir)], input=answers)
    # Re-load with the user dir pointed at the wizard output -> ready.
    monkeypatch.setattr(dest_loader, "user_destinations_dir", lambda: out_dir)
    loaded = load_destination_pack("tebra")
    assert loaded.ready is True


def test_destination_init_validate_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_dir = tmp_path / "out"
    validator = _FakeValidator(found={f"#{slot}" for slot in _ALL_SLOTS})
    monkeypatch.setattr(cli, "_make_validator", lambda cdp_url: validator)
    answers = "\n".join(_good_selectors()[slot] for slot in _ALL_SLOTS) + "\n"
    result = runner.invoke(
        app,
        [
            "destination",
            "init",
            "tebra",
            "--out-dir",
            str(out_dir),
            "--validate",
            "--cdp",
            "http://127.0.0.1:9222",
        ],
        input=answers,
    )
    assert result.exit_code == 0, result.output
    assert "found 1 element" in result.output
    # --cdp surfaces the shared-machine warning.
    assert "multi-user" in result.output.lower()
    assert (out_dir / "tebra" / "selectors.yaml").is_file()


def test_destination_init_validate_not_found_then_reentered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = tmp_path / "out"
    # Only the canonical selectors validate; a first wrong paste matches 0.
    validator = _FakeValidator(found={f"#{slot}" for slot in _ALL_SLOTS})
    monkeypatch.setattr(cli, "_make_validator", lambda cdp_url: validator)
    # For the FIRST slot, paste a bad selector first, then the good one.
    lines: list[str] = ["#WRONG", _good_selectors()[_ALL_SLOTS[0]]]
    lines += [_good_selectors()[slot] for slot in _ALL_SLOTS[1:]]
    result = runner.invoke(
        app,
        [
            "destination",
            "init",
            "tebra",
            "--out-dir",
            str(out_dir),
            "--validate",
            "--cdp",
            "http://127.0.0.1:9222",
        ],
        input="\n".join(lines) + "\n",
    )
    assert result.exit_code == 0, result.output
    assert "matched 0 elements" in result.output
    parsed = __import__("yaml").safe_load(
        (out_dir / "tebra" / "selectors.yaml").read_text(encoding="utf-8")
    )["selectors"]
    assert parsed[_ALL_SLOTS[0]] == f"#{_ALL_SLOTS[0]}"


def test_destination_init_validate_explicit_accept_unvalidated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = tmp_path / "out"
    # Nothing validates: every selector matches 0 elements.
    validator = _FakeValidator(found=set())
    monkeypatch.setattr(cli, "_make_validator", lambda cdp_url: validator)
    # First slot: 3 wrong tries, then "y" to accept unvalidated. Remaining
    # slots: 3 tries each + "y" accept. Optionals could be skipped (blank), but
    # blanks short-circuit before validation, so leave them blank.
    lines: list[str] = []
    for slot in SelectorMap.required_slots():
        lines += [f"#{slot}", f"#{slot}", f"#{slot}", "y"]  # 3 tries then accept
    for _slot in SelectorMap.optional_slots():
        lines += [""]  # blank skip (no validation on empty optional)
    result = runner.invoke(
        app,
        [
            "destination",
            "init",
            "tebra",
            "--out-dir",
            str(out_dir),
            "--validate",
            "--cdp",
            "http://127.0.0.1:9222",
        ],
        input="\n".join(lines) + "\n",
    )
    assert result.exit_code == 0, result.output
    assert "accept this unvalidated selector" in result.output
    parsed = __import__("yaml").safe_load(
        (out_dir / "tebra" / "selectors.yaml").read_text(encoding="utf-8")
    )["selectors"]
    # The explicitly-accepted (unvalidated) selectors are still written.
    assert parsed["upload_submit"] == "#upload_submit"


def test_destination_init_validate_without_cdp_errors(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["destination", "init", "tebra", "--out-dir", str(tmp_path), "--validate"],
    )
    assert result.exit_code == 2
    assert "--validate requires --cdp" in result.output


def test_destination_init_unknown_pack_is_clean_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["destination", "init", "ghost", "--out-dir", str(tmp_path)], input="\n"
    )
    assert result.exit_code == 2
    assert "no destination pack 'ghost'" in result.output


def test_unknown_explicit_source_prints_message(tmp_path: Path) -> None:
    """Regression: an unknown --source must exit 2 WITH its message — never
    silently (the refactor's error reporter originally had no else branch)."""
    result = runner.invoke(
        app,
        ["pipeline", "run", str(FIXTURE), "--out", str(tmp_path / "o"), "--source", "bogus"],
    )
    assert result.exit_code == 2
    assert "unknown source" in result.output
    assert "bogus" in result.output


def test_explicit_source_does_not_print_detected_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: 'Detected source' announces auto-detection only — an
    operator who typed --source already knows (the original behavior)."""
    pytest.importorskip("fitz", reason="needs PyMuPDF (render extra)")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    result = runner.invoke(
        app,
        ["pipeline", "run", str(FIXTURE), "--out", str(tmp_path / "o"), "--source", "pf-tebra"],
    )
    assert result.exit_code == 0, result.output
    assert "Detected source" not in result.output
