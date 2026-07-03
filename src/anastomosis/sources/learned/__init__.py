# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""Learned source adapters: formats taught from an example, executed as data.

When the toolkit meets a structured export it does not recognize, the answer is
not to fail — it is to *learn* the format from one example (the wizard in
:mod:`anastomosis.core.sourcelearn`), save a declarative ``mapping.json``, and
from then on read that format like any built-in source. This package is the
runtime half:

* :mod:`.spec` — the validated :class:`~.spec.MappingSpec` (data, never code);
* :mod:`.transforms` — the closed transform verb table;
* :mod:`.reader` — single-file IO + the column fingerprint;
* :mod:`.interpreter` — the one generic :class:`~.interpreter.LearnedSourceAdapter`
  that turns any mapping into canonical records.

Discovery is explicit, not implicit: importing this package registers nothing.
:func:`register_learned_sources` is called from the pipeline's import-time block
(like the built-in adapters), and scans the user directory then. It is
deliberately defensive — a broken or unreviewed mapping is skipped with a
PHI-safe diagnosis, never crashing discovery, and a name that collides with a
built-in adapter is skipped rather than shadowing it.

Trust is LIGHTER than template packs because a mapping carries no code: there is
no hash-gated execution. The gates are ``human_reviewed`` (a hard skip) and a
``source_trust.json`` content hash that only WARNS when a reviewed mapping was
hand-edited afterward — it never blocks loading.

PHI: this layer handles mapping ids, file paths to mapping config, and counts —
never patient data.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from anastomosis.sources.base import available_sources, register
from anastomosis.sources.learned.interpreter import LearnedSourceAdapter
from anastomosis.sources.learned.spec import SPEC_FILENAME, MappingError, MappingSpec, load_spec

__all__ = [
    "LearnedSourceAdapter",
    "MappingError",
    "MappingSpec",
    "discover_learned_specs",
    "register_learned_sources",
    "user_sources_dir",
]

logger = logging.getLogger(__name__)
_TRUST_FILE = "source_trust.json"


def user_sources_dir() -> Path:
    """The per-user directory learned mappings live in.

    A plain ``~/.anastomosis/sources`` (NOT ``platformdirs`` — no new
    dependency), matching the convention of
    :func:`anastomosis.destinations.loader.user_destinations_dir` and
    :func:`anastomosis.reconstruct.packtrust.user_pack_trust_path` so all user
    state shares one root. Each mapping is ``<here>/<mapping_id>/mapping.json``.
    """
    return Path.home() / ".anastomosis" / "sources"


def _warn_if_edited(mapping_dir: Path) -> None:
    """Warn (never block) if a reviewed mapping was hand-edited after review."""
    trust_path = mapping_dir / _TRUST_FILE
    try:
        recorded = json.loads(trust_path.read_text(encoding="utf-8")).get("mapping_sha256")
        current = hashlib.sha256((mapping_dir / SPEC_FILENAME).read_bytes()).hexdigest()
    except (OSError, ValueError):
        return  # no trust file, or unreadable — nothing to compare against
    if recorded and recorded != current:
        logger.warning(
            "learned source %r changed since it was reviewed — re-run 'anast source init' "
            "to re-verify",
            mapping_dir.name,
        )


def discover_learned_specs(base_dir: Path | None = None) -> list[MappingSpec]:
    """Load every human-reviewed mapping under ``base_dir`` (defaults to user dir).

    Defensive: a directory without a ``mapping.json``, a malformed mapping, or
    an un-reviewed mapping is skipped with a count/name-only log line — discovery
    never raises.
    """
    root = base_dir if base_dir is not None else user_sources_dir()
    if not root.is_dir():
        return []
    specs: list[MappingSpec] = []
    for mapping_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        spec_path = mapping_dir / SPEC_FILENAME
        if not spec_path.is_file():
            continue
        try:
            spec = load_spec(spec_path)
        except MappingError as exc:
            logger.warning(
                "skipping learned source in %r: %s", mapping_dir.name, type(exc).__name__
            )
            continue
        if not spec.human_reviewed:
            logger.warning("skipping un-reviewed learned source %r", mapping_dir.name)
            continue
        if spec.mapping_id != mapping_dir.name:
            logger.warning(
                "skipping learned source in %r: mapping_id %r does not match directory",
                mapping_dir.name,
                spec.mapping_id,
            )
            continue
        _warn_if_edited(mapping_dir)
        specs.append(spec)
    return specs


def register_learned_sources(base_dir: Path | None = None) -> list[str]:
    """Register an adapter for each discovered mapping; return the names added.

    A mapping whose id collides with an already-registered adapter (a built-in,
    or one registered by a prior call) is skipped, so this is safe to call more
    than once and can never shadow a built-in source.
    """
    existing = {adapter.name for adapter in available_sources()}
    added: list[str] = []
    for spec in discover_learned_specs(base_dir):
        if spec.mapping_id in existing:
            logger.warning(
                "learned source %r collides with an existing source — skipped", spec.mapping_id
            )
            continue
        register(LearnedSourceAdapter(spec))
        existing.add(spec.mapping_id)
        added.append(spec.mapping_id)
    return added
