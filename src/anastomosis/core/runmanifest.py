"""``run_manifest.json`` (53): the three profile hashes a run was bound to,
its export dir path/id, the pipeline version, and a state that moves only
prepared -> delivered -> verified through :func:`advance_state`, which
refuses on any drift and names the profile and both digests (53).
``migrate`` writes ``prepared`` only; a migration is never reported
delivered (53). No timestamps: two runs over the same inputs write
byte-identical manifests. PHI: hashes, names, version and state strings,
a receipt id, and operator-chosen paths — never a patient value.
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
    """Where a run has got to, recorded not recomputed (53): ``PREPARED``
    (nothing delivered), ``DELIVERED`` (a step returned a receipt),
    ``VERIFIED`` (that delivery was checked)."""

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
    """One bound profile whose hash disagrees with what this machine holds now."""

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
    """A stable id for the run's input directory: its resolved PATH, never
    its contents — cheap for a multi-gigabyte export, and keeps patient
    data out of the manifest. Answers "same folder?", not "same records?",
    which is the conservation ledger's question."""
    return hashlib.sha256(str(export_dir.resolve()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunManifest:
    """One run's immutable inputs plus its mutable state (53). Everything
    above :attr:`state` is fixed at preparation; :attr:`state`/
    :attr:`state_history`/:attr:`receipt` are the only fields
    :func:`advance_state` rewrites, only after :func:`verify_binding`
    passes."""

    pipeline_version: str
    source: str
    destination: str
    render_mode: str
    #: The export the run read, as a digest of its RESOLVED PATH — never the
    #: path itself. A practice that drops one folder per patient names those
    #: folders after patients, so the path is a value the chart could have
    #: given us: it is compared like any other identifier and never written
    #: down. The digest answers "was this the same export?" without saying
    #: which.
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
            binding = RunBinding.from_json(data["profiles"])
            _assert_recorded_hashes(binding, data["profiles"])
            return cls(
                pipeline_version=str(data["pipeline_version"]),
                source=str(run["source"]),
                destination=str(run["destination"]),
                render_mode=str(run["render_mode"]),
                export_dir_id=str(run["export_dir_id"]),
                binding=binding,
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


def _assert_recorded_hashes(binding: RunBinding, payload: Mapping[str, Any]) -> None:
    """Every hash the file carries must equal the hash its own payload
    produces, catching a hand-edit or a half-written file. An integrity
    check against accident, NOT a security boundary — anyone who can edit
    the file can recompute these too; the trust store and the profiles
    carry the real weight."""
    recorded = {
        "source": payload.get("source", {}).get("profile_hash"),
        "destination": payload.get("destination", {}).get("profile_hash"),
        "layout": payload.get("layout", {}).get("profile_hash"),
    }
    computed = {
        "source": binding.source.profile_hash,
        "destination": binding.destination.profile_hash,
        "layout": binding.layout.profile_hash,
    }
    wrong = sorted(name for name, value in recorded.items() if value not in (None, computed[name]))
    if payload.get("binding_hash") not in (None, binding.binding_hash):
        wrong.append("binding")
    if wrong:
        raise RunManifestError(
            "this run manifest does not agree with itself: the recorded "
            f"{', '.join(wrong)} hash(es) are not what its own contents produce. "
            "It was edited or written incompletely; re-prepare the run."
        )


def run_manifest_path(out_dir: Path) -> Path:
    """Where the run manifest lives: ``<out_dir>/run_manifest.json``, the
    run's own root beside ``charts/`` and ``ccda/`` rather than inside
    either, since it describes the whole run, not one artifact kind."""
    return out_dir / RUN_MANIFEST_NAME


def write_run_manifest(out_dir: Path, manifest: RunManifest) -> Path:
    """Write ``manifest`` into ``out_dir`` atomically (14) and owner-only
    (``0o600``): a half-written manifest would compare wrong, in the
    permissive direction, for a run that should have refused."""
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
    """The run manifest in ``out_dir``, or ``None`` when the folder holds
    none at all — an unbound run a caller must tolerate (``anast upload``
    over an older tree). A manifest PRESENT but unreadable still raises via
    :func:`read_run_manifest`: "never bound" and "bound, unreadable" are
    different answers."""
    if not run_manifest_path(out_dir).is_file():
        return None
    return read_run_manifest(out_dir)


def recapture_binding(manifest: RunManifest, *, pack_dirs: Sequence[Path] = ()) -> RunBinding:
    """Capture the three profiles again, for the identities this manifest
    names: the manifest supplies WHICH source/destination/pack, the machine
    supplies the current content, so only their hashes can differ."""
    from anastomosis.core.profiles import capture_binding, reprofile_layout

    return capture_binding(
        source=manifest.source,
        destination=manifest.destination,
        render_mode=manifest.binding.layout.render_mode,
        pack=manifest.binding.layout.pack,
        pack_dirs=pack_dirs,
        layout=reprofile_layout(manifest.binding.layout),
    )


def verify_binding(manifest: RunManifest, current: RunBinding) -> None:
    """Contract (53): raises :class:`BindingError` naming every drifted
    profile and both digests; returns ``None`` when all three match —
    there is no "matched with warnings" outcome."""
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
    """Contract (53): move the run in ``out_dir`` to ``to``, refusing on
    unbound (:class:`BindingError`), drifted (:func:`verify_binding`), or
    illegal (:class:`RunStateError`, not in :data:`_ALLOWED_TRANSITIONS`).
    ``receipt`` is the required PHI-free evidence pointer for the claim."""
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
