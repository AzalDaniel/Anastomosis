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
  required checkbox) emits the draft pack, records its content hash in the
  per-user pack-trust store, and returns its directory, hash and ``DRAFT.md``.

Where the draft lands, and why it is trusted at birth: unless the caller names
an ``out_dir``, the pack is written under
:func:`anastomosis.reconstruct.user_packs_dir` — a stable per-user location, not
a CWD-relative ``packs/`` that the next process would look for somewhere else.
Discovery hash-gates that directory, so writing the pack is not enough to make
it runnable; the SAME confirmed step records the hash of the bytes it just
wrote. That keeps the trust review explicit (a hash is still required, and any
later edit to ``context.py`` un-trusts the pack until re-confirmed) while
putting the consent where the operator actually gave it. If the hash cannot be
recorded the whole step FAILS: a draft nothing can select is exactly the
false completion this flow exists to avoid reporting as success.

When a sample page is a raster with no text objects, the analyze step asks the
offline OCR worker (:mod:`anastomosis.packgen.ocr`) — the 53-sample, 802-page
set this product was shown had zero natively extractable words, so refusing
every such page refuses the only real sample set there is. What comes back is
layout evidence carrying its own provenance, never clinical text, and the
summary and the emitted pack both say so. With no engine installed the old
refusal stands and names what to install.

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
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from anastomosis.core.logutil import exc_tag
from anastomosis.reconstruct import user_packs_dir

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
    (defaults to ``name``). ``out_dir`` is where the pack directory is written;
    ``None`` (the default) means the per-user pack directory
    (:func:`anastomosis.reconstruct.user_packs_dir`), which is where discovery
    looks regardless of the process's working directory. ``confirmed`` is the
    same-patient guard: ``False`` runs analyze-only and refuses to emit,
    ``True`` writes the draft.

    ``allow_ocr`` (default ``True``) lets the analyze step recognize sample
    pages that are pixels, using the offline engine if one is installed. It is
    a switch and not a promise: with no engine on the machine an image-only
    sample still refuses (``OcrRequiredError``), and nothing is ever
    downloaded. Setting it ``False`` is the strict native-text-only run — the
    behaviour every caller had before offline OCR existed — for an operator who
    wants a pack built from read text or from nothing.
    """

    samples: list[str]
    name: str
    display: str | None = None
    out_dir: Path | None = None
    confirmed: bool = False
    allow_ocr: bool = True


@dataclass(frozen=True)
class PackInitResult:
    """What a pack-init run yields the caller (the CLI and GUI frontends).

    ``ok`` is true only when a draft was written. ``error`` is ``None`` on
    success, else an enumerated code (``InvalidPackName``, ``NoSamplesFound``,
    ``ConfirmationRequired``) or an exception TYPE name for an analyze/emit
    failure — never a message that could embed a sample path. ``summary`` /
    ``caveat`` carry only static template text and the same-patient caveat (both
    PHI-safe). ``sample_count`` / ``low_confidence`` come from the analysis.

    ``pack_name`` / ``pack_dir`` / ``draft_md`` / ``content_hash`` are populated
    only on success. ``pack_name`` is the identity a run form offers and a run
    binds to — the manifest ``name``, which is also the directory name — and
    ``content_hash`` is the digest recorded in the pack-trust store, so a
    frontend can name the exact thing it confirmed.
    """

    ok: bool
    error: str | None
    summary: list[str]
    caveat: str
    sample_count: int
    low_confidence: bool
    pack_dir: Path | None
    draft_md: str | None
    pack_name: str | None = None
    content_hash: str | None = None


def _refused_name(name: object) -> str | None:
    """Why this name may not be taught, or ``None``.

    Type-guarded up front so a malformed command returns a code rather than
    raising — the module's "never raises into the caller" contract holds even
    for a caller that ignores the type hints. A shipped layout's name is
    refused at the same door: the hash gate would keep such a shadow honest —
    an edited one refuses rather than falls back — but the failure it sets up
    is a trap, the operator's own home quietly disabling ``generic_soap`` for
    every run, diagnosed only as "untrusted".
    """
    if not isinstance(name, str) or not PACK_NAME_RE.match(name):
        return "InvalidPackName"
    from anastomosis.reconstruct.packs import builtin_pack_names

    if name in builtin_pack_names():
        return "BuiltinPackName"
    return None


def _discard_draft(pack_dir: Path | None) -> None:
    """Remove what a failed Teach wrote, if it got as far as writing.

    A draft whose trust could not be recorded is a draft no run will accept —
    and left on disk it would claim its name in the user dir as an untrusted
    stand-in for the next discovery walk.
    """
    if pack_dir is not None:
        shutil.rmtree(pack_dir, ignore_errors=True)


def run_pack_init(cmd: PackInitCommand) -> PackInitResult:
    """Run the analyze → confirm → emit flow; return structured, PHI-safe data.

    No printing, no events — the adapter presents the returned result. ``packgen``
    is imported lazily so a minimal install imports this module cleanly. The
    same-patient guard is honored: ``cmd.confirmed=False`` runs analyze only and
    returns ``error="ConfirmationRequired"`` with the summary + caveat (writing
    nothing); ``cmd.confirmed=True`` emits the draft and returns its path +
    ``DRAFT.md``.
    """
    base = PackInitResult(
        ok=False,
        error=None,
        summary=[],
        caveat="",
        sample_count=0,
        low_confidence=False,
        pack_dir=None,
        draft_md=None,
        pack_name=None,
        content_hash=None,
    )

    if name_error := _refused_name(cmd.name):
        return replace(base, error=name_error)

    pdfs = collect_sample_pdfs(cmd.samples) if isinstance(cmd.samples, list) else []
    if not pdfs:
        return replace(base, error="NoSamplesFound")

    # Lazy import (the render extra's PyMuPDF) so a minimal install imports this
    # module cleanly — mirrors the CLI's in-function import.
    from anastomosis.packgen import analyze, extract_samples
    from anastomosis.packgen.emit import SAME_PATIENT_CAVEAT, emit_draft_pack
    from anastomosis.packgen.ocr import discover_worker

    # The offline OCR worker, or None when this machine has no engine. None is
    # not a failure here: a batch of native-text samples never asks for one,
    # and a batch that DOES ask gets OcrRequiredError naming what to install.
    # Nothing is downloaded either way.
    worker = discover_worker() if cmd.allow_ocr else None

    try:
        analysis = analyze(extract_samples(pdfs, ocr=worker))
    except Exception as exc:  # unreadable/encrypted sample — type only, no path/PHI
        return replace(base, error=exc_tag(exc), sample_count=len(pdfs))

    # The PHI-safe proposal, carried on every post-analyze outcome via
    # dataclasses.replace.
    proposal = replace(
        base,
        summary=list(analysis.summary_lines()),
        caveat=SAME_PATIENT_CAVEAT,
        sample_count=analysis.sample_count,
        low_confidence=analysis.low_confidence,
    )

    # The same-patient guard: an unconfirmed request refuses and writes nothing,
    # but still returns the PHI-safe summary so the operator sees what they are
    # being asked to confirm.
    if not cmd.confirmed:
        return replace(proposal, error="ConfirmationRequired")

    from anastomosis.reconstruct.packtrust import default_pack_trust, pack_content_hash

    out_dir = cmd.out_dir if cmd.out_dir is not None else user_packs_dir()
    pack_dir: Path | None = None
    try:
        pack_dir = emit_draft_pack(
            analysis, name=cmd.name, display=cmd.display or cmd.name, out_dir=out_dir
        )
        draft_md = (pack_dir / "DRAFT.md").read_text(encoding="utf-8")
        # The confirmed step is the consent. Recording the hash here is what
        # makes the pack selectable; it is inside the try because a draft whose
        # hash could not be recorded is a draft no run will accept, and saying
        # "written" about it would be the false completion again.
        content_hash = pack_content_hash(pack_dir)
        default_pack_trust().record(pack_dir, content_hash)
    except Exception as exc:  # emit/read/trust failure — type name only, no PHI
        _discard_draft(pack_dir)
        return replace(proposal, error=exc_tag(exc))

    return replace(
        proposal,
        ok=True,
        pack_name=cmd.name,
        pack_dir=pack_dir,
        draft_md=draft_md,
        content_hash=content_hash,
    )
