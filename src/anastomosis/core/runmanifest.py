"""The run manifest: what a run was prepared under, and what state it is in.

A migration writes charts, a C-CDA payload and an upload manifest into an output
folder, and then somebody comes back to that folder — to re-run it, to upload
from it, to check it months later. Between those two moments the machine can
change underneath: a learned mapping gets hand-edited, a template pack gets a
new ``context.py``, a destination entry gets a version bump. Nothing on disk
recorded which versions of those things the artifacts were made from, so nothing
could refuse.

``run_manifest.json`` records it. Beside the artifacts, it names:

* the **three profile hashes** the run was bound to (:mod:`anastomosis.core.profiles`);
* the run's **inputs** — the export directory the operator pointed at, by path
  and by a path-derived id; never its contents;
* the **pipeline version** that produced the artifacts;
* the **state**: :attr:`RunState.PREPARED`, and later
  :attr:`RunState.DELIVERED` / :attr:`RunState.VERIFIED`.

That last one is the difference this module makes to
:mod:`anastomosis.core.migration_status`. The classifier there derives a verdict
from a finished run's transit map every time it is asked; this records the
verdict as state that outlives the process, and advances it only through
:func:`advance_state`, which refuses when the run is unbound or when any bound
profile has drifted. ``migrate`` writes ``prepared`` and nothing else — it
executes no delivery, and the invariant that a migration is never *reported*
delivered is unchanged. What advances the state is a step that produced a
receipt.

**The refusal is the point.** :func:`verify_binding` recaptures the three
profiles from the machine as it is now and compares hash to hash. Any
difference raises :class:`BindingError` naming WHICH profile changed and both
digests. There is no fallback, no ``--best-effort``, no partial continue: a run
whose inputs changed is a different run, and continuing it into the same folder
would mix two of them.

**Determinism over a clock.** The file carries no timestamps. Two runs over the
same inputs on the same pinned environment write byte-identical manifests, which
is what makes "did anything change?" a comparison rather than a judgement — the
same rule ``upload_manifest.json`` keeps. State history is an ordered list of
state names; when a transition happened is the artifacts' own mtimes.

PHI rule: profile hashes, adapter/destination/pack names, version strings,
state names, a receipt identifier, and the operator-chosen export/output paths.
Never a patient value, and never anything read out of the export.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from anastomosis.core.profiles import RunBinding

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

__all__ = [
    "RUN_MANIFEST_NAME",
    "RUN_MANIFEST_VERSION",
    "BindingError",
    "ProfileDrift",
    "RunManifest",
    "RunManifestError",
    "RunState",
    "RunStateError",
    "advance_state",
    "export_dir_id",
    "load_run_manifest",
    "read_run_manifest",
    "recapture_binding",
    "run_manifest_path",
    "verify_binding",
    "write_run_manifest",
]

logger = logging.getLogger(__name__)

RUN_MANIFEST_NAME = "run_manifest.json"

#: Bumped when this file's shape changes. Unlike the upload manifest, there is
#: no degraded read: a manifest whose version this build does not know cannot be
#: compared against, and comparing wrongly is worse than refusing.
RUN_MANIFEST_VERSION = 1


class RunState(StrEnum):
    """Where a run has got to — recorded, not recomputed.

    ``PREPARED`` — artifacts written and a route planned; nothing delivered.
    ``DELIVERED`` — a step returned a durable receipt that the artifacts landed.
    ``VERIFIED`` — that delivery was checked against the destination.
    """

    PREPARED = "prepared"
    DELIVERED = "delivered"
    VERIFIED = "verified"


#: The only moves allowed, in order. A run cannot skip ``delivered`` on its way
#: to ``verified`` (verified means a delivery was checked, so there must be one)
#: and cannot go backwards (a folder does not become un-delivered; a new run
#: into a fresh folder is how you start over).
_ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.PREPARED: frozenset({RunState.DELIVERED}),
    RunState.DELIVERED: frozenset({RunState.VERIFIED}),
    RunState.VERIFIED: frozenset(),
}


class RunManifestError(Exception):
    """The run manifest is missing or malformed — loud, never a silent skip."""


class RunStateError(Exception):
    """An illegal state transition was asked for — names both states."""


@dataclass(frozen=True)
class ProfileDrift:
    """One bound profile whose hash no longer matches what this machine holds."""

    profile: str
    bound: str
    found: str

    def describe(self) -> str:
        """A PHI-free one-line account: which profile, and the two short hashes."""
        return f"{self.profile} profile changed (bound {self.bound[:12]}, found {self.found[:12]})"


class BindingError(Exception):
    """A bound profile changed, or the run is unbound — the refusal, never a warning.

    :attr:`drifted` carries the structured account so a frontend can name the
    profiles without scraping the sentence; the message already names them too.
    """

    def __init__(self, message: str, *, drifted: Sequence[ProfileDrift] = ()) -> None:
        super().__init__(message)
        self.drifted = tuple(drifted)


def export_dir_id(export_dir: Path) -> str:
    """A stable id for the run's input directory — its PATH, never its contents.

    Hashing the resolved path (not the files under it) is deliberate on both
    counts. It is what makes the id cheap and stable for a multi-gigabyte
    export, and it keeps every byte of patient data out of the manifest: this
    answers "was this run pointed at the same folder?", not "does that folder
    still hold the same records", which is the conservation ledger's question.
    """
    return hashlib.sha256(str(export_dir.resolve()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunManifest:
    """One run's immutable inputs plus its mutable state.

    Everything above :attr:`state` is fixed at preparation: changing any of it
    means a different run. :attr:`state`/:attr:`state_history`/:attr:`receipt`
    are the only fields :func:`advance_state` rewrites, and it does so only
    after :func:`verify_binding` passes.
    """

    pipeline_version: str
    source: str
    destination: str
    render_mode: str
    export_dir: str
    export_dir_id: str
    binding: RunBinding
    state: RunState = RunState.PREPARED
    state_history: tuple[RunState, ...] = (RunState.PREPARED,)
    #: A PHI-free pointer to the evidence behind the CURRENT state — the name of
    #: an upload run report, a ledger run id. ``None`` while ``prepared``, whose
    #: evidence is the artifacts themselves.
    receipt: str | None = None
    version: int = RUN_MANIFEST_VERSION

    @property
    def binding_hash(self) -> str:
        """The digest over the three profile hashes this run is bound to."""
        return self.binding.binding_hash

    def to_json(self) -> dict[str, Any]:
        """The manifest as it lands on disk — deterministic, no clock."""
        return {
            "version": self.version,
            "pipeline_version": self.pipeline_version,
            "state": self.state.value,
            "state_history": [state.value for state in self.state_history],
            "receipt": self.receipt,
            "run": {
                "source": self.source,
                "destination": self.destination,
                "render_mode": self.render_mode,
                "export_dir": self.export_dir,
                "export_dir_id": self.export_dir_id,
            },
            "profiles": self.binding.to_json(),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RunManifest:
        """Rebuild a manifest, raising :class:`RunManifestError` on anything odd."""
        try:
            version = int(data["version"])
            if version != RUN_MANIFEST_VERSION:
                raise RunManifestError(
                    f"run manifest version {version} is not supported by this build "
                    f"(expected {RUN_MANIFEST_VERSION})"
                )
            run = data["run"]
            history = tuple(RunState(state) for state in data["state_history"])
            return cls(
                pipeline_version=str(data["pipeline_version"]),
                source=str(run["source"]),
                destination=str(run["destination"]),
                render_mode=str(run["render_mode"]),
                export_dir=str(run["export_dir"]),
                export_dir_id=str(run["export_dir_id"]),
                binding=RunBinding.from_json(data["profiles"]),
                state=RunState(data["state"]),
                state_history=history,
                receipt=None if data.get("receipt") is None else str(data["receipt"]),
                version=version,
            )
        except RunManifestError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            # TYPE name only: a malformed manifest is a structural fault, and its
            # contents are not ours to echo.
            raise RunManifestError(f"run manifest is malformed: {type(exc).__name__}") from None


def run_manifest_path(out_dir: Path) -> Path:
    """Where the run manifest lives: ``<out_dir>/run_manifest.json``.

    The run's own root, beside ``charts/`` and ``ccda/`` rather than inside
    either — it describes the whole run, not one artifact kind, and
    ``anast upload`` is handed exactly this directory.
    """
    return out_dir / RUN_MANIFEST_NAME


def write_run_manifest(out_dir: Path, manifest: RunManifest) -> Path:
    """Write ``manifest`` into ``out_dir`` atomically and owner-only.

    Owner-only (``0o600``) and atomic for the reasons every other state file
    here is: a half-written manifest would compare wrong, and a comparison that
    is wrong in the permissive direction is a run that should have refused.
    """
    from anastomosis.core.atomic import atomic_write_text

    out_dir.mkdir(parents=True, exist_ok=True)
    target = run_manifest_path(out_dir)
    payload = json.dumps(manifest.to_json(), indent=2, sort_keys=True) + "\n"
    atomic_write_text(target, payload, mode=0o600)
    return target


def read_run_manifest(out_dir: Path) -> RunManifest:
    """Read the run manifest in ``out_dir``, raising when it is absent or bad."""
    target = run_manifest_path(out_dir)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise RunManifestError(
            f"no run manifest at {target}: this output folder is not bound to a "
            f"set of profiles ({type(exc).__name__})"
        ) from None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RunManifestError(
            f"run manifest {target} is not valid JSON: {type(exc).__name__}"
        ) from None
    if not isinstance(data, dict):
        raise RunManifestError(f"run manifest {target} must be a JSON object")
    return RunManifest.from_json(data)


def load_run_manifest(out_dir: Path) -> RunManifest | None:
    """The run manifest in ``out_dir``, or ``None`` when the folder has none.

    Distinct from :func:`read_run_manifest` on purpose. A folder with NO
    manifest is an unbound run — every output tree rendered before this file
    existed is one — and a caller that must tolerate that (``anast upload`` over
    an older tree) asks this way. A folder whose manifest is PRESENT but
    unreadable is a fault and still raises: the difference between "never bound"
    and "bound, and we cannot tell to what" is exactly the difference between
    proceeding and refusing.
    """
    if not run_manifest_path(out_dir).is_file():
        return None
    return read_run_manifest(out_dir)


def recapture_binding(manifest: RunManifest, *, pack_dirs: Sequence[Path] = ()) -> RunBinding:
    """Capture the three profiles again, for the identities this manifest names.

    The manifest supplies the IDENTITIES (which source, which destination, which
    render mode and pack); the machine supplies the current CONTENT. That split
    is what makes the comparison meaningful: the same three things are profiled,
    and only their hashes can differ.
    """
    from anastomosis.core.profiles import capture_binding

    return capture_binding(
        source=manifest.source,
        destination=manifest.destination,
        render_mode=manifest.binding.layout.render_mode,
        pack=manifest.binding.layout.pack,
        pack_dirs=pack_dirs,
    )


def verify_binding(manifest: RunManifest, current: RunBinding) -> None:
    """Refuse loudly when any profile the run was bound to has changed.

    Raises :class:`BindingError` naming every drifted profile and both digests.
    Returns ``None`` — and nothing else — when all three match: there is no
    "matched with warnings" outcome to be tempted by.
    """
    bound = manifest.binding.hashes
    found = current.hashes
    drifted = tuple(
        ProfileDrift(profile=name, bound=bound[name], found=found[name])
        for name in sorted(bound)
        if bound[name] != found[name]
    )
    if not drifted:
        return
    detail = "; ".join(drift.describe() for drift in drifted)
    raise BindingError(
        f"refusing to continue: this run was prepared under profile hashes that "
        f"have since changed — {detail}. Re-run the migration into a fresh output "
        f"folder, or restore the inputs it was prepared under.",
        drifted=drifted,
    )


def advance_state(
    out_dir: Path,
    to: RunState,
    *,
    receipt: str,
    pack_dirs: Sequence[Path] = (),
) -> RunManifest:
    """Move the run in ``out_dir`` to ``to``, or refuse — and say why.

    Three gates, in this order, and every one of them is a refusal rather than a
    downgrade:

    1. **Unbound** — no run manifest in the folder. A state cannot be recorded
       against inputs nobody wrote down; :class:`BindingError` says so.
    2. **Drifted** — a bound profile changed since preparation
       (:func:`verify_binding`). Recording ``delivered`` against artifacts whose
       inputs moved would attach a receipt to the wrong run.
    3. **Illegal** — the move is not in :data:`_ALLOWED_TRANSITIONS`
       (:class:`RunStateError`).

    ``receipt`` is the PHI-free pointer to the evidence — an upload run report
    name, a ledger run id. It is REQUIRED because that is the whole difference
    between this and a computed verdict: a state past ``prepared`` is a claim
    that something happened, and a claim needs its evidence named.
    """
    manifest = load_run_manifest(out_dir)
    if manifest is None:
        raise BindingError(
            f"refusing to record state {to.value!r}: {out_dir} holds no run manifest, "
            f"so this run is not bound to any set of profiles."
        )
    verify_binding(manifest, recapture_binding(manifest, pack_dirs=pack_dirs))
    if to not in _ALLOWED_TRANSITIONS[manifest.state]:
        allowed = ", ".join(sorted(s.value for s in _ALLOWED_TRANSITIONS[manifest.state])) or "none"
        raise RunStateError(
            f"cannot move a run from {manifest.state.value!r} to {to.value!r} "
            f"(allowed from {manifest.state.value!r}: {allowed})"
        )
    advanced = replace(
        manifest, state=to, state_history=(*manifest.state_history, to), receipt=receipt
    )
    write_run_manifest(out_dir, advanced)
    logger.info("run state advanced to %s", to.value)
    return advanced
