"""What the charts in a folder were rendered FROM, byte for byte.

``render_settings.json`` records the run's *intent*: which layout was named,
which sections were switched on, which selection rules were let through. It is
what the operator asked for. It says nothing about the bytes that answered.

That gap is real and it is quiet. A layout's ``template.html`` can be edited
after the charts were reviewed and the next run into the same folder reports
``0 rendered, 6 skipped``, exit 0 — the charts on disk came from bytes nobody is
looking at any more, and nothing on disk can name them. The content-hash trust
gate (:mod:`anastomosis.reconstruct.packtrust`) does not close it either: it
covers ``context.py``, ``template.html`` and ``pack.yaml``, so an edited asset
passes it untouched, and a re-trusted pack passes it by design.

So a render run writes a second record beside the settings — this module's
:data:`RENDER_PROVENANCE_NAME` — carrying the pack's identity (name, origin, the
same content hash the trust gate checked) and a sha256 for every file under the
pack root. Two files rather than one, deliberately:

* they answer different questions. Settings are what a person chose and can
  choose again; provenance is what the machine used and cannot be chosen. A
  reader looking for either should not have to step over the other;
* settings are compared by whole-value equality, and a folder written by an
  older build has no provenance in it. Folding hundreds of digests into that
  comparison would refuse a re-run into every directory that already exists,
  over a key that was never there;
* they fail differently. Changed settings mean "these charts answer a different
  question, re-run with --force". Changed layout bytes mean "the thing that
  produced these charts is not the thing you are holding now" — which is a
  review, not a flag.

Two digests per template, on purpose. ``files`` is measured once, before the
render; ``templates`` is what the Jinja loader actually handed the compiler
during it (:class:`RecordingLoader`). They must agree, and
:func:`swapped_templates` names any that do not — a template edited *while* a
batch was rendering means some charts came from one layout and some from
another, which is precisely the thing this file exists to make unsayable.

PHI: pack-relative paths, hex digests, a pack name and an origin word. A pack
root is operator-chosen configuration; nothing patient-derived reaches here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import FileSystemLoader

from anastomosis.core.hashutil import HASH_CHUNK_BYTES

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
    """Streamed sha256 of one file, or :data:`UNREADABLE`.

    Streamed in the same chunk size as :mod:`anastomosis.core.hashutil` so a
    pack carrying a large asset never has to be resident all at once. That
    module's :func:`~anastomosis.core.hashutil.hash_and_size` is not reused
    directly because the size is not wanted here and the unreadable case is a
    recorded value rather than a raise.
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(HASH_CHUNK_BYTES):
                digest.update(chunk)
    except OSError:
        return UNREADABLE
    return digest.hexdigest()


def pack_file_digests(root: Path) -> dict[str, str]:
    """Every file under ``root``, as ``{pack-relative posix path: sha256}``.

    The whole tree, not the three files the trust hash covers: the assets a
    layout embeds (a logo, a stylesheet, a partial) are as much a part of what a
    chart looks like as the template is, and they are exactly what the trust
    hash cannot see. Sorted, so the record is deterministic; ``__pycache__``
    left out because loading the pack creates it.
    """
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if any(part in _SKIPPED_DIRS for part in path.relative_to(root).parts):
            continue
        if not path.is_file():
            continue
        digests[path.relative_to(root).as_posix()] = _digest(path)
    return digests


@dataclass(frozen=True)
class RenderProvenance:
    """The layout a render run used, named by its bytes.

    :attr:`templates` is filled in AFTER the render from the
    :class:`RecordingLoader`, so a value built before the run carries an empty
    one; everything else is measured up front, because the re-run guard has to
    answer before any rendering happens.
    """

    pack: str
    origin: str
    content_hash: str
    files: dict[str, str]
    templates: dict[str, str] = field(default_factory=dict)

    def identity(self) -> dict[str, Any]:
        """The half two runs are compared by: who the layout is and what it holds.

        :attr:`templates` is deliberately out. It records which of :attr:`files`
        the renderer reached for, which depends on what the batch contained — a
        run that rendered nothing read no template — and a re-run guard that
        refused over that would be refusing over the input data, not the layout.
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

    Takes the LOADED pack rather than its :class:`~anastomosis.reconstruct.packs.PackStatus`:
    a status that did not load has no layout to name and the caller has already
    refused the run over it, so an optional return here would be a ``None`` no
    caller could ever see.

    The content hash is the same
    :func:`~anastomosis.reconstruct.packtrust.pack_content_hash` the trust gate
    computed, read again rather than threaded through the loader — it is three
    small files, and one function owning the definition is what keeps the number
    in this record comparable to the number in the trust store.
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

    The engine renders through ``FileSystemLoader``, so a template's ``include``
    and ``extends`` reach files the pack manifest never names. This subclass
    records every one of them by pack-relative name and sha256, which is how the
    provenance record can say what the render actually READ rather than only
    what was lying next to it.

    The digest is taken over the source re-encoded in the loader's own encoding.
    That is an exact round-trip of the bytes on disk — the loader decoded them
    with the same encoding a moment earlier, and a file that did not decode
    never got here — so it is directly comparable to
    :func:`pack_file_digests`'s digest of the same file.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(str(root))
        #: ``{pack-relative posix name: sha256}``, in the order Jinja asked.
        self.templates_read: dict[str, str] = {}

    def get_source(
        self, environment: Environment, template: str
    ) -> tuple[str, str, Callable[[], bool]]:
        source, filename, uptodate = super().get_source(environment, template)
        self.templates_read[template] = hashlib.sha256(source.encode(self.encoding)).hexdigest()
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
