"""Tests for tools.snapshot. Needs Playwright + Chromium + PyMuPDF (real
rendering); skips cleanly without them, like ``test_golden_rendering.py``."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="needs the render extra (playwright)")
pytest.importorskip("pymupdf", reason="needs the render extra (PyMuPDF)")

import pymupdf
from tools import regen_goldens
from tools.snapshot import (
    BASELINE,
    FIXTURE_GUID_PREFIXES,
    REPO_ROOT,
    _anast_env,
    _blank_special_values,
    _canonicalize_random_ids,
    _compare_extra_input,
    _looks_like_absolute_path,
    _looks_like_version,
    _parse_only,
    _restrict_baseline,
    capture_extra_input,
    compare_baseline,
    main,
    normalize_json_value,
    parse_extra_input,
    pdf_digest,
    resolved_anastomosis_module,
)


def _chromium_or_skip() -> None:
    reason = regen_goldens._renderer_available()
    if reason is not None:
        pytest.skip(reason)


# --- the load-bearing identity proof -----------------------------------------


def test_capturing_the_same_tree_twice_is_identical(tmp_path: Path) -> None:
    # ccda:pipeline alone exercises every normalizer path: JSON, the PDF-hash
    # substitution, the UUID rewrite, and XML.
    _chromium_or_skip()
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    write_first = ["--only", "ccda:pipeline", "--write-baseline", "--baseline-path", str(first)]
    write_second = ["--only", "ccda:pipeline", "--write-baseline", "--baseline-path", str(second)]
    assert main(write_first) == 0
    assert main(write_second) == 0
    assert json.loads(first.read_text()) == json.loads(second.read_text())


def test_diffing_an_unchanged_capture_against_itself_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / "baseline.json"
    assert (
        main(["--only", "ccda:pipeline", "--write-baseline", "--baseline-path", str(baseline)]) == 0
    )
    capsys.readouterr()
    assert main(["--only", "ccda:pipeline", "--baseline-path", str(baseline)]) == 0
    assert "PASSED" in capsys.readouterr().out


def test_a_tampered_baseline_is_caught_and_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half of the proof: the tool is not just quiet, it is
    SENSITIVE — a real content change in a captured deliverable is reported
    and the exit code fails the build."""
    baseline = tmp_path / "baseline.json"
    assert (
        main(["--only", "ccda:pipeline", "--write-baseline", "--baseline-path", str(baseline)]) == 0
    )
    payload = json.loads(baseline.read_text())
    ledger = payload["runs"]["ccda"]["pipeline"]["json"]["loss_ledger.json"]
    ledger["documents"] = 999
    baseline.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    capsys.readouterr()
    assert main(["--only", "ccda:pipeline", "--baseline-path", str(baseline)]) == 1
    out = capsys.readouterr().out
    assert "loss_ledger.json" in out
    assert "FAILED" in out


# --- the normalizer's pure pieces, unit-tested directly ----------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/etc/passwd", True),
        ("/tmp/x", True),
        (r"C:\Users\op\out", True),
        ("relative/path", False),
        ("Patient/feedface-0000-0000-0000-000000000001", False),
        ("", False),
    ],
)
def test_looks_like_absolute_path(value: str, expected: bool) -> None:
    assert _looks_like_absolute_path(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.7.0", True),
        ("1.2", True),
        ("1.2.3-rc1", True),
        ("a1b2c3d4e5f6", True),  # git-sha shaped
        ("1", False),
        ("not-a-version", False),
    ],
)
def test_looks_like_version(value: str, expected: bool) -> None:
    assert _looks_like_version(value) is expected


def test_blank_special_values_blanks_run_id_and_version_but_not_schema_version() -> None:
    obj = {
        "run_id": "a1b2c3d4",
        "tool_version": "0.7.0",
        "version": 1,
        "path": "/abs/path",
        "note": "relative/looking/string",
    }
    blanked = _blank_special_values(obj)
    assert blanked["run_id"] == "<run_id>"
    assert blanked["tool_version"] == "<version>"
    # An int schema version never looks like a semver/sha string - untouched.
    assert blanked["version"] == 1
    assert blanked["path"] == "<path>"
    assert blanked["note"] == "relative/looking/string"


def test_canonicalize_random_ids_rewrites_consistently_and_spares_fixture_guids() -> None:
    real_uuid = "3201335a-22c6-4bc7-a916-d47c4ccdb4c4"
    fixture_guid = f"{FIXTURE_GUID_PREFIXES[0]}0000-0000-0000-000000000001"
    obj = {
        "id": real_uuid,
        "fullUrl": f"urn:uuid:{real_uuid}",
        "patient": fixture_guid,
    }
    order: dict[str, str] = {}
    rewritten = _canonicalize_random_ids(obj, order)
    assert rewritten["id"] == rewritten["fullUrl"].removeprefix("urn:uuid:")
    assert rewritten["id"] == "<id-0>"
    assert rewritten["patient"] == fixture_guid  # fixture ids are stable, left alone


def test_normalize_json_value_is_idempotent_shaped() -> None:
    obj = {"run_id": "x", "id": "3201335a-22c6-4bc7-a916-d47c4ccdb4c4"}
    once = normalize_json_value(obj)
    twice = normalize_json_value(once)
    # A second pass over already-normalized output changes nothing further
    # (no more real UUIDs or special keys left to blank/rewrite).
    assert once == twice


# --- CLI argument parsing -----------------------------------------------------


def test_parse_only_none_means_everything() -> None:
    assert _parse_only(None) is None


def test_parse_only_restricts_fixtures_and_commands() -> None:
    selection = _parse_only("ccda:pipeline,fhir_r4")
    assert selection == {"ccda": {"pipeline"}, "fhir_r4": {"pipeline", "migrate"}}


def test_parse_only_rejects_unknown_fixture() -> None:
    from tools.snapshot import SnapshotError

    with pytest.raises(SnapshotError, match="unknown fixture"):
        _parse_only("not-a-real-fixture")


def test_parse_extra_input_requires_the_full_shape() -> None:
    from tools.snapshot import SnapshotError

    assert parse_extra_input("real=/some/dir:ccda") == ("real", Path("/some/dir"), "ccda")
    with pytest.raises(SnapshotError):
        parse_extra_input("missing-parts")


# --- comparison -----------------------------------------------------------------


def test_compare_baseline_reports_new_and_missing_fixtures() -> None:
    baseline = {"runs": {"a": {"pipeline": {"files": [], "json": {}}}}}
    current = {"runs": {"b": {"pipeline": {"files": [], "json": {}}}}}
    diffs = compare_baseline(current, baseline)
    assert any("a" in d and "missing from this run" in d for d in diffs)
    assert any("b" in d and "new fixture" in d for d in diffs)


def test_compare_baseline_reports_file_content_change() -> None:
    baseline = {
        "runs": {
            "a": {
                "pipeline": {
                    "files": [{"relpath": "x.pdf", "kind": "pdf", "digest": "aaa"}],
                    "json": {},
                }
            }
        }
    }
    current = {
        "runs": {
            "a": {
                "pipeline": {
                    "files": [{"relpath": "x.pdf", "kind": "pdf", "digest": "bbb"}],
                    "json": {},
                }
            }
        }
    }
    diffs = compare_baseline(current, baseline)
    assert any("x.pdf" in d and "content changed" in d for d in diffs)


def test_compare_baseline_is_quiet_when_nothing_changed() -> None:
    same = {
        "runs": {
            "a": {
                "pipeline": {
                    "files": [{"relpath": "x.json", "kind": "json", "digest": "aaa"}],
                    "json": {"x.json": {"k": 1}},
                }
            }
        }
    }
    assert compare_baseline(same, same) == []


def test_compare_baseline_catches_a_wrong_source_tree() -> None:
    baseline = {"runs": {}, "anastomosis_module": "src/anastomosis/__init__.py"}
    current = {"runs": {}, "anastomosis_module": "/somewhere/else/anastomosis/__init__.py"}
    diffs = compare_baseline(current, baseline)
    assert any("different tree" in d for d in diffs)


def test_compare_baseline_catches_a_missing_cli_surface() -> None:
    baseline = {"runs": {}, "cli_surface": {"help": "x"}}
    current = {"runs": {}}
    diffs = compare_baseline(current, baseline)
    assert any("cli surface missing" in d for d in diffs)


def test_compare_baseline_catches_export_dir_ids_distinct_flipping() -> None:
    baseline = {"runs": {}, "export_dir_ids_distinct": True}
    current = {"runs": {}, "export_dir_ids_distinct": False}
    diffs = compare_baseline(current, baseline)
    assert any("export_dir_ids_distinct flipped" in d for d in diffs)


def test_restrict_baseline_narrows_to_the_selection() -> None:
    baseline = {
        "runs": {
            "ccda": {"pipeline": {"files": []}, "migrate": {"files": []}},
            "fhir_r4": {"pipeline": {"files": []}},
        }
    }
    restricted = _restrict_baseline(baseline, {"ccda": {"pipeline"}})
    assert set(restricted["runs"]) == {"ccda"}
    assert set(restricted["runs"]["ccda"]) == {"pipeline"}


def test_compare_extra_input_reports_name_and_command_drift() -> None:
    prior = {"name": "x", "source": "ccda", "pipeline": {"files": [], "json": {}}}
    current = {"name": "x", "source": "fhir-r4", "pipeline": {"files": [], "json": {}}}
    diffs = _compare_extra_input(current, prior)
    assert any("source changed" in d for d in diffs)


def test_anast_env_prepends_this_checkouts_src_to_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/some/other/tree/src")
    env = _anast_env()
    parts = env["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(REPO_ROOT / "src")
    assert "/some/other/tree/src" in parts


def test_resolved_anastomosis_module_names_this_checkout() -> None:
    assert resolved_anastomosis_module() == "src/anastomosis/__init__.py"


def test_pdf_digest_catches_a_colour_only_change_word_boxes_do_not(tmp_path: Path) -> None:
    """The blocker-3 probe, in miniature: identical text and word positions,
    different fill colour — the structural (props+boxes) comparison alone
    cannot tell these apart; pdf_digest's content-stream/pixmap hashes do."""
    black = tmp_path / "black.pdf"
    red = tmp_path / "red.pdf"
    for path, color in ((black, (0, 0, 0)), (red, (1, 0, 0))):
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Hello World", color=color)
        doc.save(str(path))
        doc.close()

    props_and_boxes_equal = regen_goldens.extract_pdf_props(
        black
    ) == regen_goldens.extract_pdf_props(red) and regen_goldens.extract_word_boxes(
        black
    ) == regen_goldens.extract_word_boxes(red)
    assert props_and_boxes_equal  # same text, same positions — the old digest was blind here
    assert pdf_digest(black) != pdf_digest(red)


def test_write_baseline_with_only_requires_a_non_default_baseline_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The real committed BASELINE must never be touched by this refusal path.
    before = BASELINE.read_bytes() if BASELINE.is_file() else None
    assert main(["--only", "ccda:pipeline", "--write-baseline"]) == 2
    captured = capsys.readouterr()
    assert "requires --baseline-path" in captured.out + captured.err
    after = BASELINE.read_bytes() if BASELINE.is_file() else None
    assert before == after


def test_only_restricts_the_comparison_to_the_selected_fixtures(tmp_path: Path) -> None:
    """--only must not report every OTHER fixture as missing from this run."""
    _chromium_or_skip()
    full = tmp_path / "full.json"
    assert main(["--write-baseline", "--baseline-path", str(full)]) == 0
    assert main(["--only", "ccda:pipeline", "--baseline-path", str(full)]) == 0


def test_extra_input_first_run_writes_and_reports_nothing_compared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _chromium_or_skip()
    fixture_dir = REPO_ROOT / "tests" / "fixtures" / "ccda"
    out_dir = tmp_path / "extra"
    rc = main(["--extra-input", f"real={fixture_dir}:ccda", "--out", str(out_dir)])
    assert rc == 0
    assert "nothing compared" in capsys.readouterr().out
    assert (out_dir / "real.snapshot.json").is_file()


def test_extra_input_second_run_compares_and_passes_when_unchanged(tmp_path: Path) -> None:
    _chromium_or_skip()
    fixture_dir = REPO_ROOT / "tests" / "fixtures" / "ccda"
    out_dir = tmp_path / "extra"
    args = ["--extra-input", f"real={fixture_dir}:ccda", "--out", str(out_dir)]
    assert main(args) == 0
    assert main(args) == 0  # second run: compares against the first, unchanged


def test_extra_input_drift_fails(tmp_path: Path) -> None:
    _chromium_or_skip()
    fixture_dir = REPO_ROOT / "tests" / "fixtures" / "ccda"
    out_dir = tmp_path / "extra"
    result = capture_extra_input("real", fixture_dir, "ccda")
    target = out_dir / "real.snapshot.json"
    out_dir.mkdir(parents=True)
    result["pipeline"]["json"]["loss_ledger.json"]["documents"] = 999
    target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    assert main(["--extra-input", f"real={fixture_dir}:ccda", "--out", str(out_dir)]) == 1
