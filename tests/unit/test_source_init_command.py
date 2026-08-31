"""The shared learn-a-source command core (one flow, two frontends).

Pins the shared-core consolidation: ``resolve_example`` and the analyze -> confirm
-> build -> round-trip -> save flow live here once, returning enumerated codes
both frontends present. The CLI (`test_cli_source`) and GUI
(`test_gui_controller`) suites exercise the adapters; this pins the core directly.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from anastomosis.core.source_init_command import (
    SourceInitCommand,
    resolve_example,
    run_source_init_command,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "learned" / "clinic_visits.csv"


def test_resolve_example_file_dir_and_ambiguous(tmp_path: Path) -> None:
    assert resolve_example(FIXTURE) == (FIXTURE, "")
    assert resolve_example(tmp_path / "nope") == (None, "NoExampleFile")

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "notes.txt").write_text("x", encoding="utf-8")  # not a learnable type
    assert resolve_example(empty) == (None, "NoExampleFile")

    one = tmp_path / "one"
    one.mkdir()
    shutil.copy(FIXTURE, one / "export.csv")
    assert resolve_example(one) == (one / "export.csv", "")

    two = tmp_path / "two"
    two.mkdir()
    shutil.copy(FIXTURE, two / "a.csv")
    shutil.copy(FIXTURE, two / "b.csv")
    assert resolve_example(two) == (None, "AmbiguousExample")


def test_invalid_name_is_rejected_before_analysis() -> None:
    result = run_source_init_command(SourceInitCommand(example=FIXTURE, name="Bad-Name"))
    assert result.ok is False
    assert result.error == "InvalidSourceName"
    assert result.fmt_type is None  # never analyzed


def test_confirmation_required_returns_phi_safe_proposal(tmp_path: Path) -> None:
    result = run_source_init_command(
        SourceInitCommand(example=FIXTURE, name="clinic_csv", out_dir=tmp_path, confirmed=False)
    )
    assert result.ok is False
    assert result.error == "ConfirmationRequired"
    assert result.patient_key == "PatientID"
    assert result.fmt_type is not None
    assert result.columns > 0
    assert result.suggestions  # at least one column suggestion
    # PHI probe: the proposal carries column names/types/masked shapes only.
    blob = repr(result)
    for leak in ("Ada", "900-12-3456", "ada@example.com"):
        assert leak not in blob
    assert not (tmp_path / "clinic_csv").exists()  # nothing written


def test_confirmed_builds_round_trips_and_saves(tmp_path: Path) -> None:
    result = run_source_init_command(
        SourceInitCommand(
            example=FIXTURE,
            name="clinic_csv",
            display="Clinic CSV",
            out_dir=tmp_path,
            confirmed=True,
        )
    )
    assert result.ok is True
    assert result.error is None
    assert result.mapping_dir == tmp_path / "clinic_csv"
    assert (tmp_path / "clinic_csv" / "mapping.json").is_file()
    assert result.record_count == 3
    assert "Learned source" in (result.mapping_md or "")


def test_cannot_analyze_carries_a_phi_safe_type_detail(tmp_path: Path) -> None:
    blank = tmp_path / "blank.csv"
    blank.write_text("", encoding="utf-8")  # no header/columns
    result = run_source_init_command(
        SourceInitCommand(example=blank, name="blank", confirmed=False)
    )
    assert result.ok is False
    assert result.error == "CannotAnalyze"
    assert result.detail  # an exception TYPE name (PHI-safe), not a cell value
    assert result.fmt_type is None


def test_save_failure_carries_a_phi_safe_type_detail(tmp_path: Path) -> None:
    # out_dir is a FILE, so save_mapping's mkdir beneath it raises OSError. The
    # result names the exception TYPE (the CLI prints it), never the save path.
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("x", encoding="utf-8")
    result = run_source_init_command(
        SourceInitCommand(example=FIXTURE, name="clinic_csv", out_dir=not_a_dir, confirmed=True)
    )
    assert result.ok is False
    assert result.error == "SaveFailed"
    assert result.detail  # a PHI-safe exception type name


def test_mapping_load_failure_is_distinct_from_dropped(tmp_path: Path) -> None:
    # A mapped column whose transform chokes (DOB -> parse_date over a non-date)
    # is a fixable MappingLoadFailed, NOT an unexplained WouldDropColumns.
    bad = tmp_path / "bad.csv"
    bad.write_text("PID,DOB\np1,garbage-not-a-date\n", encoding="utf-8")
    result = run_source_init_command(
        SourceInitCommand(example=bad, name="bad_src", out_dir=tmp_path, confirmed=True)
    )
    assert result.ok is False
    assert result.error == "MappingLoadFailed"
    assert "DOB" in (result.detail or "")  # names the column (PHI-safe)
    assert "garbage-not-a-date" not in repr(result)  # the value never leaks
    assert not (tmp_path / "bad_src").exists()  # nothing written


# --- an id somebody else already answers to (#334, #333) ---------------------


def test_a_builtin_id_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    """Saving a learned format as `ccda` must fail, and fail EARLY.

    It used to succeed. The save reported ok, wrote the whole directory, and
    registration then skipped the mapping to a log line because a learned source
    may never shadow a built-in — correctly, since `get_source("ccda")` has to
    stay the C-CDA adapter. The operator got a success message, a folder full of
    their reviewed work, and a format they could never select: not then, and not
    after a restart, because the skip is permanent by design.

    Refused from the NAME, before the example is even analysed, because the
    collision is knowable from the name alone and there is no reason to spend
    somebody's attention on a proposal they will not be allowed to keep.
    """
    result = run_source_init_command(
        SourceInitCommand(example=FIXTURE, name="ccda", out_dir=tmp_path, confirmed=True)
    )
    assert result.ok is False
    assert result.error == "SourceIdReserved"
    assert result.fmt_type is None, "the example was analysed despite a doomed name"
    assert list(tmp_path.iterdir()) == [], "a refused save left a partial mapping behind"


def test_an_already_learned_id_is_refused_rather_than_overwritten(tmp_path: Path) -> None:
    """A second teach under the same name must not silently replace the first.

    A mapping decides how a source file's columns become patient identity and
    encounter data, and somebody reviewed the one already there. Overwriting it
    on the strength of a matching string would discard that decision without
    asking, and would leave the in-memory adapter answering for a mapping that
    no longer exists on disk.

    A distinct code from the built-in case because they are different situations
    for the person reading them: one can never be used, the other is their own
    earlier work.
    """
    first = run_source_init_command(
        SourceInitCommand(example=FIXTURE, name="reteach_me", out_dir=tmp_path, confirmed=True)
    )
    assert first.ok is True

    again = run_source_init_command(
        SourceInitCommand(example=FIXTURE, name="reteach_me", out_dir=tmp_path, confirmed=True)
    )
    assert again.ok is False
    assert again.error == "SourceIdInUse"
    # And the first mapping is untouched — refusing protected it.
    assert (tmp_path / "reteach_me" / "mapping.json").is_file()


def test_a_saved_format_is_selectable_without_a_restart(tmp_path: Path) -> None:
    """Teach a format and Charts can offer it NOW.

    `pipeline` registers learned sources once, at import. A format taught
    mid-session was written, valid and loadable, and simply invisible: the
    choosers are populated from the registry and nothing had told the registry
    to look again. The GUI said the format was available; it was not, and no
    message anywhere said a restart was needed, because nothing in the code
    knew one was.

    Asserted on the ADAPTER, not on a matching name — a source answering to the
    right string is the check that passed while the behaviour was wrong.
    """
    from anastomosis.sources import get_source
    from anastomosis.sources.learned import LearnedSourceAdapter

    result = run_source_init_command(
        SourceInitCommand(example=FIXTURE, name="selectable_now", out_dir=tmp_path, confirmed=True)
    )
    assert result.ok is True

    adapter = get_source("selectable_now")
    assert isinstance(adapter, LearnedSourceAdapter), (
        "the saved mapping is not the adapter this session would run"
    )
    assert adapter.name == "selectable_now"
