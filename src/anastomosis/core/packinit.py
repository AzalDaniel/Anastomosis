"""The shared pack-from-samples command layer (one flow, two frontends).

``anast pack init`` (:mod:`anastomosis.cli`) and the GUI's
:meth:`anastomosis.gui.controller.GuiController.pack_init` both run the SAME
analyze → confirm → emit flow against :mod:`anastomosis.packgen`. That flow
lives here exactly once, so the same operator intent produces identical backend
state regardless of which frontend issued it.

Mirrors the command-layer style of :mod:`anastomosis.core.commands` /
:mod:`anastomosis.core.migrate`: frozen dataclass commands, a ``run_*``
function returning structured, presentation-free data. It never prints and
never emits — each adapter presents the returned :class:`PackInitResult` (the
CLI's Rich lines, the GUI's dict + events).

The two-step shape the frontends share:

* ``confirmed=False`` runs only ANALYZE — it returns the PHI-safe summary and
  the same-patient caveat (``error="ConfirmationRequired"``) so the operator
  sees exactly what they are confirming, and writes NOTHING.
* ``confirmed=True`` (the CLI's interactive ``typer.confirm``, the GUI's
  required checkbox) emits the draft pack and returns its directory plus the
  ``DRAFT.md`` text.

PHI rule (non-negotiable): nothing patient-derived is returned. ``summary``
carries only static template text (recurring across distinct samples — labels /
headings by construction) and counts; ``sample_count`` is a count, never a
path. The pack config that *is* returned (``pack_dir``, ``draft_md`` — static
template provenance) is not PHI. On any failure the error is an exception TYPE
name (:func:`anastomosis.core.logutil.exc_tag`) or an enumerated code, never a
message that could embed a sample path.

``packgen`` (and its PyMuPDF dependency) is imported lazily INSIDE
:func:`run_pack_init`, so a minimal install imports this module cleanly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from anastomosis.core.logutil import exc_tag

__all__ = [
    "LOW_SAMPLE_FLOOR",
    "PACK_NAME_RE",
    "PackInitCommand",
    "PackInitResult",
    "collect_sample_pdfs",
    "run_pack_init",
]

# A pack name must be a safe directory + manifest identifier (it becomes the
# pack's directory name and YAML ``name:``). Mirrors the loader's expectations
# and governs a learned-source mapping id too.
PACK_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Below this many samples the static/per-patient split is statistically weak;
# the frontend warns loudly (the learner still runs).
LOW_SAMPLE_FLOOR = 3


def collect_sample_pdfs(patterns: list[str]) -> list[Path]:
    """Resolve dir-or-glob arguments into a sorted, de-duplicated PDF list.

    A bare directory contributes its ``*.pdf`` children; a glob is expanded;
    a direct path is taken as-is. Sorted for deterministic sample indices.
    """
    import glob as _glob

    found: set[Path] = set()
    for raw in patterns:
        candidate = Path(raw)
        if candidate.is_dir():
            found.update(p for p in candidate.glob("*.pdf"))
            continue
        if candidate.is_file():
            found.add(candidate)
            continue
        # Treat as a glob (supports ``./samples/*.pdf``).
        found.update(Path(match) for match in _glob.glob(raw) if Path(match).is_file())
    return sorted(found)


@dataclass(frozen=True)
class PackInitCommand:
    """A fully-specified pack-init request — the unit both frontends build.

    ``samples`` is a list of dir-or-glob-or-file arguments (resolved by
    :func:`collect_sample_pdfs`). ``name`` is the lowercase manifest identifier
    (validated against :data:`PACK_NAME_RE`); ``display`` is the human label
    (defaults to ``name``). ``out_dir`` is where the pack directory is written.
    ``confirmed`` is the same-patient guard: ``False`` runs analyze-only and
    refuses to emit, ``True`` writes the draft.
    """

    samples: list[str]
    name: str
    display: str | None = None
    out_dir: Path = Path("packs")
    confirmed: bool = False


@dataclass(frozen=True)
class PackInitResult:
    """What a pack-init run yields the caller (the CLI and GUI frontends).

    ``ok`` is true only when a draft was written. ``error`` is ``None`` on
    success, else an enumerated code (``InvalidPackName``, ``NoSamplesFound``,
    ``ConfirmationRequired``) or an exception TYPE name for an analyze/emit
    failure — never a message that could embed a sample path. ``summary`` /
    ``caveat`` carry only static template text and the same-patient caveat (both
    PHI-safe). ``sample_count`` / ``low_confidence`` come from the analysis.
    ``pack_dir`` / ``draft_md`` are populated only on success.
    """

    ok: bool
    error: str | None
    summary: list[str]
    caveat: str
    sample_count: int
    low_confidence: bool
    pack_dir: Path | None
    draft_md: str | None


def run_pack_init(cmd: PackInitCommand) -> PackInitResult:
    """Run the analyze → confirm → emit flow; return structured, PHI-safe data.

    No printing, no events — the adapter presents the returned result. ``packgen``
    is imported lazily so a minimal install imports this module cleanly. The
    same-patient guard is honored: ``cmd.confirmed=False`` runs analyze only and
    returns ``error="ConfirmationRequired"`` with the summary + caveat (writing
    nothing); ``cmd.confirmed=True`` emits the draft and returns its path +
    ``DRAFT.md``.
    """
    # Type-guard up front so a malformed command returns a code rather than
    # raising — the module's "never raises into the caller" contract holds even
    # for a caller that ignores the type hints.
    if not isinstance(cmd.name, str) or not PACK_NAME_RE.match(cmd.name):
        return PackInitResult(
            ok=False,
            error="InvalidPackName",
            summary=[],
            caveat="",
            sample_count=0,
            low_confidence=False,
            pack_dir=None,
            draft_md=None,
        )

    pdfs = collect_sample_pdfs(cmd.samples) if isinstance(cmd.samples, list) else []
    if not pdfs:
        return PackInitResult(
            ok=False,
            error="NoSamplesFound",
            summary=[],
            caveat="",
            sample_count=0,
            low_confidence=False,
            pack_dir=None,
            draft_md=None,
        )

    # Lazy import (the render extra's PyMuPDF) so a minimal install imports this
    # module cleanly — mirrors the CLI's in-function import.
    from anastomosis.packgen import analyze, extract_samples
    from anastomosis.packgen.emit import SAME_PATIENT_CAVEAT, emit_draft_pack

    try:
        analysis = analyze(extract_samples(pdfs))
    except Exception as exc:  # unreadable/encrypted sample — type only, no path/PHI
        return PackInitResult(
            ok=False,
            error=exc_tag(exc),
            summary=[],
            caveat="",
            sample_count=len(pdfs),
            low_confidence=False,
            pack_dir=None,
            draft_md=None,
        )

    summary = list(analysis.summary_lines())
    caveat = SAME_PATIENT_CAVEAT

    # The same-patient guard: an unconfirmed request refuses and writes nothing,
    # but still returns the PHI-safe summary so the operator sees what they are
    # being asked to confirm.
    if not cmd.confirmed:
        return PackInitResult(
            ok=False,
            error="ConfirmationRequired",
            summary=summary,
            caveat=caveat,
            sample_count=analysis.sample_count,
            low_confidence=analysis.low_confidence,
            pack_dir=None,
            draft_md=None,
        )

    try:
        pack_dir = emit_draft_pack(
            analysis, name=cmd.name, display=cmd.display or cmd.name, out_dir=cmd.out_dir
        )
        draft_md = (pack_dir / "DRAFT.md").read_text(encoding="utf-8")
    except Exception as exc:  # emit/read failure — type name only, no PHI
        return PackInitResult(
            ok=False,
            error=exc_tag(exc),
            summary=summary,
            caveat=caveat,
            sample_count=analysis.sample_count,
            low_confidence=analysis.low_confidence,
            pack_dir=None,
            draft_md=None,
        )

    return PackInitResult(
        ok=True,
        error=None,
        summary=summary,
        caveat=caveat,
        sample_count=analysis.sample_count,
        low_confidence=analysis.low_confidence,
        pack_dir=pack_dir,
        draft_md=draft_md,
    )
