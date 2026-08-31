"""The layout you taught it: from the Teach step to a run that uses it.

Teaching a document layout used to end at "the draft layout was written to
packs/acme_soap". The next two screens could not offer it — discovery never
looked at a relative ``packs/``, and would not have executed its ``context.py``
if it had — so the operator confirmed one layout and then ran a different one.
These tests walk that whole handoff: where the draft lands, what confirming it
grants, whether the choosers offer it afterwards and from a different working
directory, and what a run does when the layout is missing, edited, or untrusted.

Everything here is synthetic: sample PDFs are drawn with PyMuPDF from invented
patients, the chart fixture is the checked-in synthetic export, and the user
home is the per-test temporary one the suite's conftest installs.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf", reason="the Teach flow needs the render extra (PyMuPDF)")

import anastomosis.reconstruct.chromium as chromium  # noqa: E402
from anastomosis.core.packinit import PackInitCommand, run_pack_init  # noqa: E402
from anastomosis.gui.controller import GuiController  # noqa: E402
from anastomosis.pipeline import RENDER_SETTINGS_NAME, PipelineError  # noqa: E402
from anastomosis.reconstruct import discover_packs, user_packs_dir  # noqa: E402
from anastomosis.reconstruct.packtrust import (  # noqa: E402
    default_pack_trust,
    pack_content_hash,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"

LEARNED = "acme_soap"

# Four invented patients, each value unique across the samples so nothing
# patient-derived can look like the layout's own static wording.
_PATIENTS = (
    ("Synthia Example", "03/14/1985", "Hypertension follow-up"),
    ("Maxwell Sample", "07/04/1952", "Diabetes review"),
    ("Cleo Placeholder", "12/01/2021", "Well child visit"),
    ("Dale Specimen", "09/09/1970", "Annual physical"),
)


class _Sink:
    """A controller event sink that records nothing — these tests read state."""

    def emit(self, event: dict[str, object]) -> None:
        pass


class _FakeChromium:
    """Writes a REAL pdf carrying the rendered text (the test_cli.py pattern)."""

    def __init__(self, **kwargs: object) -> None:
        pass

    def render(self, html: str, pdf_path: Path) -> None:
        from _render_fakes import write_text_pdf

        write_text_pdf(html, pdf_path)

    def close(self) -> None:
        pass


def _samples(tmp_path: Path) -> Path:
    """A directory of distinct-patient synthetic sample PDFs to teach from."""
    samples = tmp_path / "samples"
    samples.mkdir()
    for index, (name, dob, complaint) in enumerate(_PATIENTS):
        doc = pymupdf.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((60, 90), "SUBJECTIVE", fontsize=13, fontname="hebo")
        page.insert_text((60, 130), "OBJECTIVE", fontsize=13, fontname="hebo")
        page.insert_text((60, 200), "DOB:", fontsize=11, fontname="helv")
        page.insert_text((200, 200), dob, fontsize=11, fontname="helv")
        page.insert_text((60, 260), f"Patient {name} seen today.", fontsize=11, fontname="helv")
        page.insert_text((60, 280), complaint, fontsize=11, fontname="helv")
        doc.save(str(samples / f"sample{index}.pdf"))
        doc.close()
    return samples


def _teach(tmp_path: Path) -> dict[str, object]:
    """Run the GUI's Teach step to completion and return its answer."""
    result = GuiController(_Sink()).pack_init(str(_samples(tmp_path)), LEARNED, None, True)
    assert result["ok"] is True, result
    return result


def _available(controller: GuiController) -> dict[str, dict[str, object]]:
    """The layouts a run form would offer, by name (what the choosers filter to)."""
    info = controller.info()
    assert info["ok"] is True
    packs = info["packs"]
    assert isinstance(packs, list)
    return {str(p["name"]): p for p in packs if p["available"]}  # type: ignore[index]


# --- where the draft lands --------------------------------------------------


def test_the_draft_lands_in_the_user_home_not_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The destination is a property of the operator, not of the process's CWD.

    ``out_dir`` defaulted to a relative ``packs``, so where a taught layout went
    depended on where the app was launched from — and where discovery looked
    later did not depend on that at all.
    """
    workdir = tmp_path / "somewhere-else"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    written = Path(str(_teach(tmp_path)["pack_dir"]))

    assert written == user_packs_dir() / LEARNED
    assert written.is_absolute()
    assert not (workdir / "packs").exists(), "nothing may be written beside the process"


def test_an_explicit_destination_is_still_honored(tmp_path: Path) -> None:
    """Naming a directory still writes there — the default changed, not the option."""
    chosen = tmp_path / "chosen"
    result = run_pack_init(
        PackInitCommand(
            samples=[str(_samples(tmp_path))], name=LEARNED, out_dir=chosen, confirmed=True
        )
    )
    assert result.ok is True
    assert result.pack_dir == chosen / LEARNED


# --- consent -----------------------------------------------------------------


def test_confirming_the_teach_is_what_trusts_the_code_it_wrote(tmp_path: Path) -> None:
    """The confirmed step records the hash of the bytes it just wrote.

    The trust review is not skipped — the hash gate is still the only thing that
    lets a learned ``context.py`` run — but the consent is taken where the
    operator gave it, rather than demanded again on a later screen that never
    offered the layout in the first place.
    """
    written = Path(str(_teach(tmp_path)["pack_dir"]))

    assert default_pack_trust().is_trusted(written, pack_content_hash(written))


def test_the_teach_reports_the_identity_a_run_will_bind_to(tmp_path: Path) -> None:
    """Name, directory and hash come back — the three ways to name one layout."""
    result = _teach(tmp_path)

    assert result["pack"] == LEARNED
    assert result["content_hash"] == pack_content_hash(Path(str(result["pack_dir"])))


def test_an_unconfirmed_teach_writes_nothing_and_trusts_nothing(tmp_path: Path) -> None:
    """The same-patient gate still comes first; refusing leaves no trace."""
    result = GuiController(_Sink()).pack_init(str(_samples(tmp_path)), LEARNED, None, False)

    assert result["error"] == "ConfirmationRequired"
    assert not (user_packs_dir() / LEARNED).exists()


# --- the handoff to the choosers --------------------------------------------


def test_a_taught_layout_is_offered_by_both_run_forms(tmp_path: Path) -> None:
    """Charts and Migrate populate from the same ``info()`` the Teach step feeds.

    This is the reported defect stated as an assertion: after a Teach that
    reported success, the layout was in neither chooser.
    """
    _teach(tmp_path)

    offered = _available(GuiController(_Sink()))

    assert LEARNED in offered, f"the taught layout is not selectable: {sorted(offered)}"
    assert offered[LEARNED]["origin"] == "user"
    assert offered[LEARNED]["root"] == str(user_packs_dir() / LEARNED)
    # The built-ins are still there — a learned layout is an addition.
    assert "generic_soap" in offered


def test_a_fresh_process_in_a_fresh_directory_still_offers_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restart coverage: a new controller, asked from a different CWD.

    The Teach runs in one directory and the question is asked from another, by a
    controller that shares nothing with the one that wrote the pack — which is
    all "the app was restarted" means to this layer.
    """
    taught_from = tmp_path / "launched-here"
    taught_from.mkdir()
    monkeypatch.chdir(taught_from)
    _teach(tmp_path)

    asked_from = tmp_path / "launched-there"
    asked_from.mkdir()
    monkeypatch.chdir(asked_from)
    assert Path(os.getcwd()) == asked_from

    assert LEARNED in _available(GuiController(_Sink()))


# --- the run binds to the exact pack ----------------------------------------


def test_charts_render_through_the_layout_that_was_taught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Select the learned layout on the Charts form and the run uses THAT one."""
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    _teach(tmp_path)
    out = tmp_path / "charts"

    result = GuiController(_Sink()).run_pipeline(str(FIXTURE), str(out), pack=LEARNED)

    assert result["ok"] is True, result
    import json

    settings = json.loads((out / RENDER_SETTINGS_NAME).read_text(encoding="utf-8"))
    assert settings["pack"] == LEARNED, "the run recorded a different layout than the one chosen"
    assert result["rendered"] == 6


def test_a_migration_prepares_through_the_layout_that_was_taught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Migrate form offers the same layout, and naming it renders through it."""
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    _teach(tmp_path)
    out = tmp_path / "migration"

    result = GuiController(_Sink()).run_migration(
        str(FIXTURE), str(out), "pf-tebra", "athenahealth", render=LEARNED
    )

    assert result["ok"] is True, result
    import json

    settings = json.loads((out / "charts" / RENDER_SETTINGS_NAME).read_text(encoding="utf-8"))
    assert settings["pack"] == LEARNED
    assert settings["pack"] != "generic_soap"


def test_a_deleted_layout_refuses_the_run_rather_than_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A layout that is gone stops the run. It never becomes the neutral one.

    Silently rendering somebody's charts through a layout they did not choose is
    the same false completion as the one this issue names, moved to where it
    costs more.
    """
    import shutil

    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    _teach(tmp_path)
    shutil.rmtree(user_packs_dir() / LEARNED)

    result = GuiController(_Sink()).run_pipeline(
        str(FIXTURE), str(tmp_path / "charts"), pack=LEARNED
    )

    assert result["ok"] is False
    assert LEARNED in str(result["error"])
    assert not list((tmp_path / "charts").glob("*.pdf"))


def test_an_edited_layout_is_refused_until_it_is_confirmed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing ``context.py`` un-trusts the pack — for the choosers and the run.

    This is the trust review the issue insists on keeping: consent was given for
    a specific set of bytes, and whoever changed them afterwards did not have
    it.
    """
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    _teach(tmp_path)
    context = user_packs_dir() / LEARNED / "context.py"
    context.write_text(context.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")

    status = discover_packs(trust=default_pack_trust())[LEARNED]
    assert status.available is False
    assert status.diagnosis is not None and "untrusted" in status.diagnosis
    assert status.root == user_packs_dir() / LEARNED

    assert LEARNED not in _available(GuiController(_Sink()))
    result = GuiController(_Sink()).run_pipeline(
        str(FIXTURE), str(tmp_path / "charts"), pack=LEARNED
    )
    assert result["ok"] is False


def test_the_pipeline_refuses_an_untrusted_layout_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core raises ``bad_pack`` rather than resolving to anything else."""
    from anastomosis.pipeline import run_pipeline

    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    _teach(tmp_path)
    context = user_packs_dir() / LEARNED / "context.py"
    context.write_text(context.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")

    with pytest.raises(PipelineError) as caught:
        run_pipeline(
            export_dir=FIXTURE,
            out=tmp_path / "charts",
            source=None,
            pack=LEARNED,
            pack_dirs=None,
            force=False,
            section=None,
            qa=False,
        )
    assert caught.value.kind == "bad_pack"
    assert caught.value.exit_code == 2


# --- discovery never runs code it cannot vouch for --------------------------


def test_a_learned_pack_is_never_executed_without_a_trust_store(tmp_path: Path) -> None:
    """No store, no execution — the hash is the only thing that authorizes it.

    A caller that checks no trust store cannot show the code is the code that
    was confirmed, so the pack is diagnosed rather than imported. The planted
    ``context.py`` writes a file if it ever runs, so this is evidence and not an
    inference from the diagnosis text.
    """
    planted = user_packs_dir() / "planted_layout"
    planted.mkdir(parents=True)
    (planted / "pack.yaml").write_text(
        "name: planted_layout\ndisplay: Planted\ntemplate: template.html\n", encoding="utf-8"
    )
    (planted / "template.html").write_text("<html></html>", encoding="utf-8")
    tripwire = tmp_path / "context-was-executed"
    (planted / "context.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(tripwire)!r}).write_text('executed')\n"
        "def build_context(*args, **kwargs):\n    return {}\n",
        encoding="utf-8",
    )

    status = discover_packs()["planted_layout"]

    assert status.available is False
    assert not tripwire.exists(), "an untrusted learned pack's context.py was executed"


# --- the two seams a learned layout must not disturb ------------------------


def test_the_install_self_check_asks_only_about_the_shipped_layouts(tmp_path: Path) -> None:
    """A user layout shadowing a built-in name cannot fail the asset check.

    Shadowing is a documented thing an operator may do, and the self-check's
    question is whether the SHIPPED files are present and readable. Asking it of
    a directory the operator owns turned one legitimate choice into a reported
    broken install.
    """
    shadow = user_packs_dir() / "generic_soap"
    shadow.mkdir(parents=True)
    (shadow / "pack.yaml").write_text("name: generic_soap\n", encoding="utf-8")

    checks = GuiController(_Sink()).doctor()["checks"]
    assert isinstance(checks, list)
    by_name = {str(c["name"]): c for c in checks}  # type: ignore[index]

    assert by_name["built-in packs"]["ok"] is True


def test_upload_verification_re_reads_a_trusted_learned_layout(tmp_path: Path) -> None:
    """L3 reads header fields from the learned layout that rendered the charts.

    The pack is not copied into the output tree, so the upload side re-discovers
    it by the name the manifest recorded. It carries executable code, so it
    loads only while it still matches the hash the Teach recorded — trusted, it
    is read; edited, L3 skips rather than reading a layout nobody confirmed.
    """
    from anastomosis.core.upload_command import _verification_pack

    _teach(tmp_path)

    loaded = _verification_pack(LEARNED)
    assert loaded is not None
    assert loaded.root == user_packs_dir() / LEARNED

    context = user_packs_dir() / LEARNED / "context.py"
    context.write_text(context.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    assert _verification_pack(LEARNED) is None


# --- the review's three findings, pinned -------------------------------------


def test_trust_pack_consent_does_not_reach_the_user_dir(tmp_path: Path) -> None:
    """--trust-pack names the --pack-dir packs; an edited learned layout is
    re-trusted by one act only, re-confirming a Teach.

    The review's probe: an operator consenting to a VENDOR pack must not
    silently re-trust — and execute — whatever bytes now sit in their user
    dir under a taught layout's name."""
    _teach(tmp_path)
    context = user_packs_dir() / LEARNED / "context.py"
    context.write_text(context.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")

    statuses = discover_packs([], trust=default_pack_trust(), trust_new=True)
    status = statuses[LEARNED]
    assert status.pack is None, "an edited learned layout was re-trusted by --trust-pack"
    assert "re-confirm the Teach" in status.diagnosis

    # The flag still does its own job: a --pack-dir pack IS trusted on first use.
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    import shutil

    shutil.copytree(
        Path(__file__).resolve().parents[2] / "src" / "anastomosis" / "packs" / "generic_soap",
        vendor / "vendor_soap",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    manifest = vendor / "vendor_soap" / "pack.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("name: generic_soap", "name: vendor_soap"),
        encoding="utf-8",
    )
    statuses = discover_packs(
        [vendor], allow_external=True, trust=default_pack_trust(), trust_new=True
    )
    assert statuses["vendor_soap"].pack is not None


def test_a_teach_may_not_claim_a_shipped_layouts_name(tmp_path: Path) -> None:
    """Teaching "generic_soap" refuses at the door and writes nothing — the
    alternative is the operator's own home standing in front of a shipped
    layout, diagnosed only as "untrusted" every run thereafter."""
    result = run_pack_init(
        PackInitCommand(
            samples=[str(_samples(tmp_path))], name="generic_soap", display=None, confirmed=True
        )
    )
    assert result.error == "BuiltinPackName"
    assert not (user_packs_dir() / "generic_soap").exists()


def test_an_unavailable_shadow_names_what_it_stands_in_front_of(tmp_path: Path) -> None:
    """A pre-existing user dir under a shipped layout's name keeps its refusal
    — running the built-in instead would be the forbidden fallback — but the
    diagnosis says which directory has displaced what."""
    shadow = user_packs_dir() / "generic_soap"
    shadow.mkdir(parents=True)
    (shadow / "context.py").write_text("VALUES = {}\n", encoding="utf-8")
    (shadow / "pack.yaml").write_text('name: generic_soap\ndisplay: "Shadow"\n', encoding="utf-8")

    statuses = discover_packs([], trust=default_pack_trust())
    status = statuses["generic_soap"]
    assert status.pack is None, "an untrusted shadow ran, or the built-in silently won"
    assert "standing in front of the built-in" in status.diagnosis


def test_a_teach_that_cannot_record_trust_removes_what_it_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed trust record fails the Teach AND removes the written draft —
    otherwise the failure leaves an untrusted stand-in claiming the name."""
    import anastomosis.reconstruct.packtrust as packtrust

    class _Refuses:
        def record(self, root: Path, content_hash: str) -> None:
            raise OSError("store not writable")

    monkeypatch.setattr(packtrust, "default_pack_trust", lambda: _Refuses())
    result = run_pack_init(
        PackInitCommand(
            samples=[str(_samples(tmp_path))], name=LEARNED, display=None, confirmed=True
        )
    )
    assert result.error == "OSError"
    assert not (user_packs_dir() / LEARNED).exists(), "the failed Teach left its draft behind"
