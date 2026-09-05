"""Tests for tools.snapshot. Needs Playwright + Chromium + PyMuPDF (real
rendering); skips cleanly without them, like ``test_golden_rendering.py``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="needs the render extra (playwright)")
pytest.importorskip("pymupdf", reason="needs the render extra (PyMuPDF)")

from tools import regen_goldens
from tools.snapshot import (
    FIXTURE_GUID_PREFIXES,
    _blank_special_values,
    _canonicalize_random_ids,
    _looks_like_absolute_path,
    _looks_like_version,
    _parse_only,
    compare_baseline,
    main,
    normalize_json_value,
    parse_extra_input,
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
