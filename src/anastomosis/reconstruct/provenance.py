"""What the charts in a folder were rendered FROM, byte for byte (RULES.md 26).

Kept separate from ``render_settings.json`` (intent, whole-value comparable):
changed settings mean re-run with --force, changed layout bytes mean the run
is not trustworthy. ``files`` is measured before the render; ``templates``
is what Jinja actually read during it — :func:`swapped_templates` names any
mismatch (a template edited mid-batch).

PHI: pack-relative paths, hex digests, a pack name and origin word only.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import FileSystemLoader

from anastomosis.core.hashutil import file_sha256

if TYPE_CHECKING:
    from jinja2 import Environment

    from anastomosis.reconstruct.packs import LoadedPack

__all__ = [
    "RENDER_PROVENANCE_NAME",
    "RecordingLoader",
    "RenderProvenance",
    "pack_file_digests",
    "pack_provenance",
    "provenance_difference",
    "swapped_templates",
]

#: Where a run publishes the bytes its charts were produced from. Beside the
#: charts, like ``render_settings.json`` and ``loss_ledger.json``, so a folder
#: carried to another machine carries its own account of itself.
RENDER_PROVENANCE_NAME = "render_provenance.json"

#: Schema version of that file. Bumped when a field's meaning changes; the
#: re-run guard compares recorded identity to current identity, so a version it
#: does not recognise is treated as "nothing comparable here" rather than as a
#: mismatch (see :func:`provenance_difference`).
PROVENANCE_VERSION = 1

#: A file whose bytes could not be read. Recorded rather than dropped — an
#: unreadable file inside a pack is a fact about the render, and a record that
#: silently omitted it would compare equal to a run where the file was fine.
UNREADABLE = "unreadable"

#: Directories that are build output rather than layout. ``__pycache__`` in
#: particular is written BY loading the pack, so a run that included it would
#: disagree with the run before it for a reason that has nothing to do with the
#: layout.
_SKIPPED_DIRS = frozenset({"__pycache__"})


def _digest(path: Path) -> str:
    """One file's streamed sha256, or :data:`UNREADABLE` — a value the record
    carries rather than a raise that would drop the file from it."""
    return file_sha256(path, unreadable=UNREADABLE)


def pack_file_digests(root: Path) -> dict[str, str]:
    """Every file under ``root``, as ``{pack-relative posix path: sha256}``.

    Contract: the WHOLE tree (not just the trust-hashed three files), sorted,
    ``__pycache__`` excluded. Symlinked directories are FOLLOWED — ``rglob``
    treats a symlink-to-dir as a file and drops its subtree, which would make
    an asset edited through a linked directory invisible here while a
    template reached through it still renders, reading as a mid-batch swap.
    The walk tracks real (device, inode) pairs to guard the cycle risk
    following introduces.
    """
    digests: dict[str, str] = {}
    seen: set[tuple[int, int]] = set()
    for parent, dirnames, filenames in os.walk(root, followlinks=True):
        here = Path(parent)
        key = _dir_identity(here)
        if key is not None:
            if key in seen:
                dirnames[:] = []
                continue
            seen.add(key)
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIPPED_DIRS)
        if any(part in _SKIPPED_DIRS for part in here.relative_to(root).parts):
            continue
        for name in filenames:
            path = here / name
            if path.is_file():
                digests[path.relative_to(root).as_posix()] = _digest(path)
    return dict(sorted(digests.items()))


def _dir_identity(path: Path) -> tuple[int, int] | None:
    """``(device, inode)`` for a directory, or ``None`` when it cannot be read:
    the cycle guard for the followed walk, since a link pointing at an ancestor
    would otherwise recurse until the path length gave out."""
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


@dataclass(frozen=True)
class RenderProvenance:
    """The layout a render run used, named by its bytes.

    :attr:`templates` is filled in AFTER the render; a value built before
    carries an empty one. Everything else is measured up front, since the
    re-run guard must answer before rendering starts.
    """

    pack: str
    origin: str
    content_hash: str
    files: dict[str, str]
    templates: dict[str, str] = field(default_factory=dict)

    def identity(self) -> dict[str, Any]:
        """The half two runs are compared by: who the layout is and what it holds.

        :attr:`templates` is deliberately out: it depends on what the batch
        contained, so comparing it would refuse over the input data, not
        the layout.
        """
        return {
            "pack": self.pack,
            "origin": self.origin,
            "content_hash": self.content_hash,
            "files": dict(sorted(self.files.items())),
        }

    def as_json(self) -> dict[str, Any]:
        """The whole record as it lands on disk."""
        return {
            "version": PROVENANCE_VERSION,
            **self.identity(),
            "templates": dict(sorted(self.templates.items())),
        }

    def with_templates(self, templates: dict[str, str]) -> RenderProvenance:
        """This record plus what the renderer actually read."""
        return RenderProvenance(
            pack=self.pack,
            origin=self.origin,
            content_hash=self.content_hash,
            files=self.files,
            templates=dict(templates),
        )


def pack_provenance(pack: LoadedPack, origin: str) -> RenderProvenance:
    """Measure the layout a run is about to render through.

    Takes the LOADED pack, not its ``PackStatus`` — a status that failed to
    load has no layout to name, and the caller has already refused the run.
    The content hash is recomputed via
    :func:`~anastomosis.reconstruct.packtrust.pack_content_hash`, the same
    one the trust gate used, so the two numbers stay comparable.
    """
    from anastomosis.reconstruct.packtrust import pack_content_hash

    return RenderProvenance(
        pack=pack.manifest.name,
        origin=origin,
        content_hash=pack_content_hash(pack.root),
        files=pack_file_digests(pack.root),
    )


class RecordingLoader(FileSystemLoader):
    """A Jinja file loader that remembers the bytes it handed the compiler.

    Records every template Jinja resolves (``include``/``extends`` reach
    files the pack manifest never names) by pack-relative name and sha256,
    so provenance can say what the render actually READ. The digest is taken
    over the file's BYTES, re-read from the resolved path — Jinja opens
    templates in text mode, and re-encoding the decoded source would
    disagree with :func:`pack_file_digests`'s binary digest for any CRLF
    template.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(str(root))
        #: ``{pack-relative posix name: sha256}``, in the order Jinja asked.
        self.templates_read: dict[str, str] = {}

    def get_source(
        self, environment: Environment, template: str
    ) -> tuple[str, str, Callable[[], bool]]:
        source, filename, uptodate = super().get_source(environment, template)
        self.templates_read[template] = _digest(Path(filename))
        return source, filename, uptodate


def swapped_templates(provenance: RenderProvenance) -> list[str]:
    """Templates whose rendered bytes are not the bytes measured before the run.

    Non-empty means the layout was edited WHILE the batch was rendering, so the
    charts in the output directory do not all come from one layout. Names only —
    the caller decides how loudly to fail.
    """
    return sorted(
        name
        for name, digest in provenance.templates.items()
        if provenance.files.get(name) != digest
    )


def _file_differences(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Which pack files changed, appeared, or went away, by name."""
    was = {str(k): str(v) for k, v in (previous.get("files") or {}).items()}
    now = {str(k): str(v) for k, v in (current.get("files") or {}).items()}
    return (
        [f"{name} changed" for name in sorted(was.keys() & now.keys()) if was[name] != now[name]]
        + [f"{name} added" for name in sorted(now.keys() - was.keys())]
        + [f"{name} removed" for name in sorted(was.keys() - now.keys())]
    )


#: How many changed file names a refusal names before it starts counting. Enough
#: to see the shape of an edit; short enough to stay one readable sentence.
_NAMED_FILES = 5


def provenance_difference(previous: dict[str, Any], current: dict[str, Any]) -> str:
    """One PHI-free sentence naming how the recorded layout differs from this one.

    Empty when they are the same layout — including when ``previous`` carries no
    version this build understands, which is a folder written by another build
    rather than a folder written by another layout.
    """
    if previous.get("version") != PROVENANCE_VERSION:
        return ""
    changes: list[str] = []
    for key, label in (("pack", "layout"), ("origin", "layout origin")):
        if previous.get(key) != current.get(key):
            changes.append(f"{label} {previous.get(key)!r} -> {current.get(key)!r}")
    files = _file_differences(previous, current)
    if files:
        named = ", ".join(files[:_NAMED_FILES])
        rest = len(files) - _NAMED_FILES
        changes.append(named + (f", and {rest} more" if rest > 0 else ""))
    elif previous.get("content_hash") != current.get("content_hash"):
        # Reachable only if the two records disagree about the hash while
        # agreeing about every file — a defect in one of them, and silence
        # would be the worst answer.
        changes.append("the layout's recorded content hash no longer matches its files")
    return "; ".join(changes)
