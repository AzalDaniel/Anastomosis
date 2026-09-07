"""The shared pack-from-samples command layer: one analyze -> confirm ->
emit flow for the CLI and the GUI (28), returning structured data and
never printing. ``confirmed=True`` is the consent (28); it records the
draft's hash in the trust store and discards the draft if that fails (29).
Drafts land under the per-user pack dir unless ``out_dir`` is given (36).
OCR is layout evidence only, never clinical truth (34). ``packgen`` (and
PyMuPDF) imports lazily inside :func:`run_pack_init` so a minimal install
stays clean.
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
    """Contract: a fully-specified pack-init request, the unit both
    frontends build. ``out_dir=None`` means the per-user pack directory
    (36); ``confirmed`` is the same-patient guard (28). ``allow_ocr`` is a
    switch, not a promise: with no engine installed an image-only sample
    still refuses (``OcrRequiredError``), never downloading one."""

    samples: list[str]
    name: str
    display: str | None = None
    out_dir: Path | None = None
    confirmed: bool = False
    allow_ocr: bool = True


@dataclass(frozen=True)
class PackInitResult:
    """Contract: what a pack-init run yields. ``ok`` is true only when a
    draft was written. ``error`` is ``None`` on success, else an enumerated
    code or exception TYPE name, never a message that could embed a sample
    path. ``pack_name``/``pack_dir``/``draft_md``/``content_hash`` are
    populated only on success."""

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
    """Why this name may not be taught, or ``None``. Type-guarded up front
    so a malformed command returns a code rather than raising. A built-in
    pack's name is refused at the same door — shadowing it would disable
    that pack for every run, diagnosed only as "untrusted"."""
    if not isinstance(name, str) or not PACK_NAME_RE.match(name):
        return "InvalidPackName"
    from anastomosis.reconstruct.packs import builtin_pack_names

    if name in builtin_pack_names():
        return "BuiltinPackName"
    return None


def _discard_draft(pack_dir: Path | None) -> None:
    """Remove what a failed emit wrote, if it got that far (29): left on
    disk it would claim its name in the user dir as an untrusted stand-in
    for the next discovery walk."""
    if pack_dir is not None:
        shutil.rmtree(pack_dir, ignore_errors=True)


def run_pack_init(cmd: PackInitCommand) -> PackInitResult:
    """Contract: run analyze -> confirm -> emit (28); return structured,
    PHI-safe data, no printing. ``cmd.confirmed=False`` returns
    ``error="ConfirmationRequired"`` with the summary and writes nothing;
    ``True`` emits the draft and returns its path plus ``DRAFT.md``."""
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
