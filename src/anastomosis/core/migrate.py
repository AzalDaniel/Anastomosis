"""The shared EHR-to-EHR migration core (one migration, two frontends).

A migration is a general EHR→EHR move; the PF→Tebra path is just one instance
of it. The honest output model this realizes:

* **structured C-CDA is the primary cross-EHR payload** — the artifact the
  target EHR imports and renders natively (``deliver.ccda_export.deliver_ccda``);
* the **rendered PDF** is the human-readable archive/fallback, in an
  operator-chosen *representation* (a neutral SOAP pack, the HL7 standard C-CDA
  view, or a vendor Jinja skin).

So every migration emits BOTH: a ``ccda`` payload directory and a ``charts``
directory. The route the destination would take is resolved up front
(:func:`anastomosis.deliver.router.plan_route`) and surfaced to the operator as
a transit map — the same data the wizard draws.

This module is frontend-free (no Typer, no Rich, no webview), mirroring
:mod:`anastomosis.pipeline` / :mod:`anastomosis.core.commands`: it emits the
SAME PHI-safe :class:`~anastomosis.pipeline.StageEvent`\\ s the pipeline emits
(so each frontend's presenter works unchanged) and raises
:class:`~anastomosis.pipeline.PipelineError` on loud failures.

Three render modes resolve as:

* ``"neutral"`` → the built-in ``generic_soap`` Jinja pack (the neutral default);
* ``"ccda-standard"`` → the HL7-stylesheet standard C-CDA view, one PDF per
  patient (:func:`anastomosis.reconstruct.ccda_standard.render_ccda_standard`),
  with NO Jinja pack at all;
* any other string → a Jinja pack name (e.g. ``"practice_fusion_soap"``).

PHI rule: events/logs carry counts, stage names, ids, and exception type names
only — never patient-derived values. :class:`MigrationProfiles` stores config
(source/destination/render/sections/qa) only — never export paths, never PHI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from anastomosis.core.commands import DeliveryOutcome
    from anastomosis.core.model import PatientRecord
    from anastomosis.deliver.router import TransitMap
    from anastomosis.pipeline import EventSink, PipelineResult
    from anastomosis.reconstruct.ccda_standard import CCDARenderResult

__all__ = [
    "RENDER_CCDA_STANDARD",
    "RENDER_NEUTRAL",
    "MigrationCommand",
    "MigrationProfiles",
    "MigrationResult",
    "run_migration",
    "user_migrations_path",
]

# The two named render modes. Anything else is taken as a Jinja pack name.
RENDER_NEUTRAL = "neutral"
RENDER_CCDA_STANDARD = "ccda-standard"

# The Jinja pack the neutral mode renders through (the neutral default).
_NEUTRAL_PACK = "generic_soap"


@dataclass(frozen=True)
class MigrationCommand:
    """A fully-specified migration — the unit both frontends build.

    ``source`` (the ``--from`` adapter name) is REQUIRED: a migration is
    explicit, never auto-detected — the operator declares both ends.
    ``destination`` is the ``--to`` registry name (resolved to a transit map).
    ``render`` selects the human-readable representation: ``"neutral"``,
    ``"ccda-standard"``, or a Jinja pack name. ``export_dir`` / ``out_dir`` are
    per-run paths (NOT persisted to a profile).
    """

    export_dir: Path
    out_dir: Path
    source: str
    destination: str
    render: str = RENDER_NEUTRAL
    pack_dirs: tuple[Path, ...] = ()
    trust_new: bool = False
    force: bool = False
    sections: Mapping[str, bool] = field(default_factory=dict)
    qa: bool = True


@dataclass
class MigrationResult:
    """What a migration run yields the caller (the CLI and GUI frontends).

    ``ccda_export`` (the structured payload) is ALWAYS present — it is what the
    target EHR imports. ``pipeline`` is the full pipeline result in neutral/pack
    mode and ``None`` in ccda-standard mode; ``ccda_view`` is the standard-view
    render result in ccda-standard mode and ``None`` otherwise. ``pack`` is the
    resolved Jinja pack name, or ``None`` in ccda-standard mode.
    """

    transit: TransitMap
    pipeline: PipelineResult | None
    ccda_view: CCDARenderResult | None
    ccda_export: DeliveryOutcome
    render_mode: str
    pack: str | None
    # The canonical records processed (same list in both render modes), so a
    # frontend can show per-patient detail (names/DOB/note counts) in every mode.
    # Local display only — never logged or emitted on an event.
    records: list[PatientRecord]


def _charts_dir(out_dir: Path) -> Path:
    return out_dir / "charts"


def _ccda_dir(out_dir: Path) -> Path:
    return out_dir / "ccda"


def run_migration(cmd: MigrationCommand, on_event: EventSink | None = None) -> MigrationResult:
    """Run a migration: resolve the route, render the charts, emit the C-CDA payload.

    Emits the SAME PHI-safe :class:`~anastomosis.pipeline.StageEvent`\\ s the
    pipeline emits (so the CLI/GUI presenters work unchanged) and raises
    :class:`~anastomosis.pipeline.PipelineError` on loud failures. The structured
    C-CDA payload lands in ``<out>/ccda``; the human-readable charts in
    ``<out>/charts``.
    """
    from anastomosis.deliver.router import plan_route
    from anastomosis.destinations.registry import DestinationRegistry
    from anastomosis.pipeline import PipelineError

    # Resolve the transit map up front. An unknown destination is an operator
    # typo (exit 2) — surface it as a clean PipelineError, never a traceback.
    try:
        transit = plan_route(cmd.destination, DestinationRegistry.load())
    except KeyError as exc:
        raise PipelineError(
            str(exc.args[0] if exc.args else exc), exit_code=2, kind="bad_destination"
        ) from None

    if cmd.render == RENDER_CCDA_STANDARD:
        return _run_ccda_standard(cmd, transit, on_event)
    return _run_pack_mode(cmd, transit, on_event)


def _run_pack_mode(
    cmd: MigrationCommand, transit: TransitMap, on_event: EventSink | None
) -> MigrationResult:
    """Neutral / Jinja-pack mode: the full pipeline + a ccda delivery.

    The chart representation is a Jinja pack (``generic_soap`` for ``"neutral"``,
    else the named pack), and the structured payload rides the standard ``ccda``
    deliverer — so this reuses :func:`run_pipeline_command` verbatim (locking,
    output validation, QA, event emission) rather than re-implementing it.
    """
    from anastomosis.core.commands import DeliveryCommand, PipelineCommand, run_pipeline_command

    pack = _NEUTRAL_PACK if cmd.render == RENDER_NEUTRAL else cmd.render
    out = cmd.out_dir
    result = run_pipeline_command(
        PipelineCommand(
            export_dir=cmd.export_dir,
            charts_dir=_charts_dir(out),
            source=cmd.source,
            pack=pack,
            pack_dirs=cmd.pack_dirs,
            force=cmd.force,
            trust_new=cmd.trust_new,
            sections=cmd.sections,
            qa=cmd.qa,
            deliveries=(DeliveryCommand("ccda", _ccda_dir(out)),),
        ),
        on_event=on_event,
    )
    return MigrationResult(
        transit=transit,
        pipeline=result.pipeline,
        ccda_view=None,
        ccda_export=result.deliveries["ccda"],
        render_mode=cmd.render,
        pack=pack,
        records=result.pipeline.records,
    )


def _run_ccda_standard(
    cmd: MigrationCommand, transit: TransitMap, on_event: EventSink | None
) -> MigrationResult:
    """Standard-C-CDA-view mode: no Jinja pack — render HL7's own view per patient.

    There is no pack pipeline here, so this loads records once via the source
    adapter (mirroring :func:`anastomosis.pipeline.resolve_source` +
    ``adapter.load`` and emitting the same DETECT/INGEST events), renders the
    standard C-CDA view into ``<out>/charts``, and writes the structured payload
    into ``<out>/ccda``. Output dirs are validated up front (exit-2 on a file
    collision) and the charts dir is held under the same advisory lock
    :func:`run_pipeline_command` uses.
    """
    from anastomosis.core.commands import DeliveryOutcome
    from anastomosis.core.locking import OutputLockedError, output_lock
    from anastomosis.core.output import OutputPathError, validate_output_target
    from anastomosis.deliver.ccda_export import deliver_ccda
    from anastomosis.pipeline import (
        STAGE_DETECT,
        STAGE_INGEST,
        PipelineError,
        StageEvent,
        resolve_source,
    )
    from anastomosis.reconstruct.ccda_standard import render_ccda_standard

    emit = on_event or (lambda _event: None)
    out = cmd.out_dir
    charts = _charts_dir(out)
    ccda = _ccda_dir(out)

    # Pre-flight BOTH output targets before any work, so a path that is actually
    # a file fails cleanly (exit 2) rather than raising a raw OSError deep in a
    # renderer/deliverer.
    for target in (charts, ccda):
        try:
            validate_output_target(target)
        except OutputPathError as exc:
            raise PipelineError(str(exc), exit_code=2, kind="bad_output") from None

    adapter = resolve_source(cmd.export_dir, cmd.source)
    emit(StageEvent(STAGE_DETECT, detail=adapter.name))

    try:
        with output_lock(charts):
            records = list(adapter.load(cmd.export_dir))
            emit(StageEvent(STAGE_INGEST, counts={"records": len(records)}))

            view = render_ccda_standard(records, charts, force=cmd.force)
            if view.failed:
                # Loud render failure, mirroring the pipeline's render_failed
                # kind so the CLI reproduces its per-patient detail lines.
                raise PipelineError(
                    f"{len(view.failed)} patient(s) failed to render",
                    exit_code=1,
                    kind="render_failed",
                    failed=tuple(view.failed),
                )

            paths = deliver_ccda(records, ccda)
    except OutputLockedError as exc:
        raise PipelineError(str(exc), exit_code=2, kind="output_locked") from None

    ccda_export = DeliveryOutcome(kind="ccda", out_dir=ccda, counts={"patients": len(paths)})
    return MigrationResult(
        transit=transit,
        pipeline=None,
        ccda_view=view,
        ccda_export=ccda_export,
        render_mode=cmd.render,
        pack=None,
        records=records,
    )


# --- profile persistence ------------------------------------------------------
#
# A migration profile saves the REUSABLE config of a migration — source,
# destination, render representation, section flags, QA — so an operator runs a
# recurring migration by name. It deliberately does NOT save the per-run paths
# (export_dir / out_dir), and it carries no PHI: every value is a vendor
# identifier, a pack/render name, a section flag, or a boolean.


def user_migrations_path() -> Path:
    """The per-user migration-profiles store path.

    A plain ``~/.anastomosis/migrations.json`` (NOT ``platformdirs`` — no new
    dependency), matching
    :func:`anastomosis.destinations.loader.user_destinations_dir` and
    :func:`anastomosis.reconstruct.packtrust.user_pack_trust_path` so all
    Anastomosis user state lives under one root.
    """
    return Path.home() / ".anastomosis" / "migrations.json"


# The config keys a profile carries — config only, never paths, never PHI.
_PROFILE_KEYS: tuple[str, ...] = ("source", "destination", "render", "sections", "qa")


class MigrationProfiles:
    """A JSON store of named migration profiles (config only — no paths, no PHI).

    The store is ``{"<name>": {"source", "destination", "render", "sections",
    "qa"}}``. Mirrors :class:`anastomosis.reconstruct.packtrust.PackTrust`:
    defensive load (a missing or garbage store starts empty), atomic write, and
    owner-only (``0o600``) on POSIX.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._store: dict[str, dict[str, object]] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Missing or garbage store → start empty (a corrupt profile file
            # simply offers no profiles; it never crashes a run).
            return
        if isinstance(data, dict):
            self._store = {
                str(name): dict(profile)
                for name, profile in data.items()
                if isinstance(profile, dict)
            }

    def get(self, name: str) -> dict[str, object] | None:
        """Return the profile named ``name`` (a copy), or ``None`` if absent."""
        profile = self._store.get(name)
        return dict(profile) if profile is not None else None

    def names(self) -> list[str]:
        """Sorted profile names."""
        return sorted(self._store)

    def save(self, name: str, profile: dict[str, object]) -> None:
        """Persist ``profile`` under ``name`` and write the store.

        Only the config keys (:data:`_PROFILE_KEYS`) are stored — any stray
        keys (e.g. a path) are dropped, keeping the store PHI-free by
        construction. The write is atomic (a temp file is written then
        ``os.replace``\\ d into place, so a crash mid-write never corrupts an
        existing store) and owner-only from creation on POSIX (the temp is
        opened ``0o600``, leaving no umask-mode window).
        """
        self._store[name] = {key: profile[key] for key in _PROFILE_KEYS if key in profile}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._store, indent=2, sort_keys=True) + "\n"
        tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        try:
            if os.name == "posix":
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
            else:  # pragma: no cover - POSIX is the tested platform
                tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)  # never leave a stray temp on failure
            raise


def default_migration_profiles() -> MigrationProfiles:
    """The :class:`MigrationProfiles` backed by :func:`user_migrations_path`."""
    return MigrationProfiles(user_migrations_path())
