"""The reconstruction engine: canonical records → chart PDFs via a pack.

Behaviors this engine guarantees:

* **Renderer recycling** — Chromium leaks slowly; the engine retires and
  relaunches the renderer every N renders instead of debugging that.
* **Crash relaunch** — a renderer crash mid-run costs one retry, not the
  batch.
* **Collision suffixing** — two same-day visits resolve to the same
  filename; the loser gets a source-id suffix rather than overwriting, and
  the suffix widens until the name is genuinely free (the same-day-visit
  defense). Two encounters carrying one id have no name that separates
  them: that chart is reported as a failure, never quietly overwritten.
* **Idempotent skip** — re-running a half-finished batch only renders what
  is missing, so interruption is always safe.

Failures are recorded as exception *types* only (PHI-safe logging rule);
the run report never embeds patient-derived text.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from jinja2 import Environment, FileSystemLoader

from anastomosis.core.atomic import atomic_replace
from anastomosis.core.logutil import exc_tag, safe_log_id
from anastomosis.core.model import Encounter, PatientRecord
from anastomosis.core.output import secure_output_dir
from anastomosis.core.textutil import safe_name

from .chromium import RendererUnavailable
from .packs import LoadedPack

__all__ = [
    "DuplicateEncounterId",
    "ReconstructionEngine",
    "RenderResult",
    "RenderedDoc",
    "Renderer",
]

logger = logging.getLogger(__name__)


class DuplicateEncounterId(Exception):
    """Two encounters share one id, so no filename can tell their charts apart.

    Carries the id and nothing else: an encounter id is a source identifier,
    not patient text, and callers log it through ``safe_log_id``.
    """

    def __init__(self, encounter_id: str) -> None:
        super().__init__(f"two encounters share id {safe_log_id(encounter_id)}")
        self.encounter_id = encounter_id


class Renderer(Protocol):
    """Turns one HTML document into one PDF file."""

    def render(self, html: str, pdf_path: Path) -> None: ...

    def close(self) -> None: ...


RendererFactory = Callable[[], Renderer]


@dataclass(frozen=True)
class RenderedDoc:
    """What QA needs to verify a document against its source record."""

    path: Path
    encounter_id: str
    patient_id: str


@dataclass
class RenderResult:
    rendered: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    # (encounter_id, exception type name) — never exception text.
    failed: list[tuple[str, str]] = field(default_factory=list)
    documents: list[RenderedDoc] = field(default_factory=list)


class ReconstructionEngine:
    def __init__(
        self,
        pack: LoadedPack,
        renderer_factory: RendererFactory,
        *,
        recycle_every: int = 250,
        section_overrides: dict[str, bool] | None = None,
    ) -> None:
        self._pack = pack
        self._factory = renderer_factory
        self._recycle_every = recycle_every
        self._renderer: Renderer | None = None
        self._renders_since_launch = 0
        # Effective section flags: manifest defaults, then user choices from
        # the section-selection matrix.
        self.section_flags: dict[str, bool] = {
            key: flag.default for key, flag in pack.manifest.sections.items()
        }
        self.section_flags.update(section_overrides or {})
        # The clock this engine renders in. It already reaches the pack through
        # `cfg["timezone"]` below; QA needs the same value to work out which day
        # "today" was for these documents, and reading it off the manifest a
        # second time is how the two drift apart.
        self.timezone: str = pack.manifest.timezone
        self._env = Environment(
            loader=FileSystemLoader(pack.root), autoescape=True, keep_trailing_newline=True
        )

    # --- renderer lifecycle ---

    def _acquire_renderer(self) -> Renderer:
        if self._renderer is None:
            self._renderer = self._factory()
            self._renders_since_launch = 0
        return self._renderer

    def _retire_renderer(self) -> None:
        if self._renderer is not None:
            try:
                self._renderer.close()
            except Exception as exc:
                logger.warning("renderer close failed (%s)", exc_tag(exc))
            self._renderer = None

    def _after_render(self) -> None:
        self._renders_since_launch += 1
        if self._renders_since_launch >= self._recycle_every:
            logger.info("recycling renderer after %d renders", self._renders_since_launch)
            self._retire_renderer()

    # --- naming ---

    def _filename_for(self, encounter: Encounter, record: PatientRecord) -> str:
        dos = encounter.date_of_service
        fields = {
            "family": safe_name(record.patient.family_name, "Unknown"),
            "given": safe_name(record.patient.given_name, "Unknown"),
            "dos": dos.strftime("%m-%d-%Y") if dos else "undated",
            "type": safe_name(encounter.note_type, "note"),
        }
        return self._pack.manifest.filename.pattern.format(**fields)

    def _allocate_target(
        self, out_dir: Path, name: str, encounter: Encounter, claimed: set[Path]
    ) -> Path:
        """Deterministic name allocation: collisions are resolved against the
        names claimed *this run* (iteration order is stable), so a re-run
        allocates identical names and the idempotent skip works. The loser of
        a same-day collision gets a source-id suffix — never an overwrite.

        The suffix used to be the id's first eight characters, applied once,
        with no check that the suffixed name was free either. Ids that agree on
        those eight characters are not exotic — sequential Millennium
        ``ENCNTR_ID``s share long prefixes by construction, and every GUID in
        this repo's own pf_tebra fixture starts ``feedface`` — so a third
        same-day visit landed on the second and took it with it. Hence the
        widening loop: eight characters, then the whole id, which two distinct
        encounters cannot share.
        """
        stem, ext = Path(name).stem, Path(name).suffix
        ident = encounter.id.replace("-", "")
        for candidate in (
            out_dir / name,
            out_dir / f"{stem}-{ident[:8]}{ext}",
            out_dir / f"{stem}-{ident}{ext}",
        ):
            if candidate not in claimed:
                claimed.add(candidate)
                return candidate
        # Only two encounters carrying ONE id reach here, and there is no name
        # left that tells them apart. Refusing costs this chart; guessing would
        # lose one silently, which is the thing this tool must never do.
        raise DuplicateEncounterId(encounter.id)

    # --- the run ---

    def run(
        self, records: Iterable[PatientRecord], out_dir: str | Path, *, force: bool = False
    ) -> RenderResult:
        out = secure_output_dir(out_dir)
        template = self._env.get_template(self._pack.template_path.name)
        result = RenderResult()
        claimed: set[Path] = set()
        try:
            for record in records:
                # One cache dict per record, shared across that record's
                # encounters: a pack memoizes its record-level groupings here so
                # they are built ONCE per record, not once per encounter. The
                # content is pack-specific; the seam is not. Output is unchanged.
                record_cache: dict[str, Any] = {}
                for encounter in record.encounters:
                    self._render_one(
                        encounter, record, template, out, force, claimed, result, record_cache
                    )
        finally:
            self._retire_renderer()
        # Persist the render index so downstream deliverers (archive, bundle) can attribute
        # PDFs by patient_id directly, instead of reverse-inferring ownership from the leading
        # ``{family}_{given}_`` prefix — two same-name patients would misattribute silently.
        # Written even on partial/empty runs: an empty index is still a truthful "attribution
        # known" answer.
        self._write_render_index(out, result)
        return result

    @staticmethod
    def _write_render_index(out: Path, result: RenderResult) -> None:
        from anastomosis.deliver.render_index import (
            RenderEntry,
            RenderIndex,
            RenderIndexConflict,
        )

        try:
            index = RenderIndex.from_entries(
                RenderEntry(
                    pdf=doc.path.name,
                    patient_id=doc.patient_id,
                    encounter_id=doc.encounter_id,
                )
                for doc in result.documents
            )
        except RenderIndexConflict as exc:
            # Unreachable by construction now that _allocate_target widens
            # until the name is free — kept because it is the last place a
            # future allocator bug could still be caught, and writing an index
            # that maps two encounters to one file would hide it again.
            logger.error("render index would self-conflict (%s); not written", exc_tag(exc))
            return
        try:
            index.write(out)
        except OSError as exc:
            # A failed sidecar must not crash the render — log loud and
            # let the deliverers fall back to fail-closed (unattributed/).
            logger.warning("render index write failed (%s)", exc_tag(exc))

    def _render_one(
        self,
        encounter: Encounter,
        record: PatientRecord,
        template: Any,
        out: Path,
        force: bool,
        claimed: set[Path],
        result: RenderResult,
        record_cache: dict[str, Any],
    ) -> None:
        try:
            target = self._allocate_target(
                out, self._filename_for(encounter, record), encounter, claimed
            )
        except DuplicateEncounterId as exc:
            # One unnameable chart, reported like any other render failure: the
            # count and the exit code carry it, and the other several thousand
            # charts in the batch still get written.
            logger.error(
                "cannot name a chart for encounter %s (%s)",
                safe_log_id(encounter.id),
                exc_tag(exc),
            )
            result.failed.append((encounter.id, exc_tag(exc)))
            return
        if target.exists() and not force:
            result.skipped.append(target)
            # Skipped is not unverified: QA re-checks existing documents too,
            # so a corrupted file never survives a re-run unnoticed.
            result.documents.append(RenderedDoc(target, encounter.id, record.patient.id))
            return
        cfg = {
            "sections": self.section_flags,
            "timezone": self._pack.manifest.timezone,
            "tokens": self._pack.manifest.tokens,
            "record_cache": record_cache,
        }
        try:
            context = self._pack.build_context(encounter, record, cfg)
            html = template.render(**context)
            self._render_pdf(html, target)
            result.rendered.append(target)
            result.documents.append(RenderedDoc(target, encounter.id, record.patient.id))
        except RendererUnavailable:
            # Not this chart's failure — the machine cannot render any chart, so
            # tagging it per encounter produced N identical "(RuntimeError)"
            # lines and discarded the one sentence that said what to install.
            # Raised once, ends the run, message intact.
            raise
        except Exception as exc:
            logger.error(
                "render failed for encounter %s (%s)", safe_log_id(encounter.id), exc_tag(exc)
            )
            result.failed.append((encounter.id, exc_tag(exc)))

    def _render_pdf(self, html: str, target: Path) -> None:
        # Render to a sibling temp file, then atomically replace the target, so
        # a crash mid-write (or a concurrent reader) never sees a partial PDF.
        with atomic_replace(target) as tmp:
            try:
                self._acquire_renderer().render(html, tmp)
            except RendererUnavailable:
                # Nothing to relaunch. The retry below is for a Chromium that
                # died mid-run; a machine with no browser at all will not have
                # one on the second attempt, and retrying only adds a "renderer
                # crashed" warning in front of the message that says what to
                # install.
                raise
            except Exception as exc:
                # Crash relaunch: one fresh renderer, one retry, then report.
                logger.warning("renderer crashed (%s); relaunching once", exc_tag(exc))
                self._retire_renderer()
                self._acquire_renderer().render(html, tmp)
        self._after_render()
