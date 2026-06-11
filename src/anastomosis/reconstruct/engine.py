"""The reconstruction engine: canonical records → chart PDFs via a pack.

Ported behaviors from the predecessor (which reconstructed 12,906 PDFs at
100% final QA), re-typed clean:

* **Renderer recycling** — Chromium leaks slowly; the engine retires and
  relaunches the renderer every N renders instead of debugging that.
* **Crash relaunch** — a renderer crash mid-run costs one retry, not the
  batch.
* **Collision suffixing** — two same-day visits resolve to the same
  filename; the loser gets a short source-id suffix rather than
  overwriting (the same-day-visit defense).
* **Idempotent skip** — re-running a half-finished batch only renders what
  is missing, so interruption is always safe.

Failures are recorded as exception *types* only (PHI-safe logging rule);
the run report never embeds patient-derived text.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from jinja2 import Environment, FileSystemLoader

from anastomosis.core.logutil import exc_tag
from anastomosis.core.model import Encounter, PatientRecord
from anastomosis.core.output import secure_output_dir

from .packs import LoadedPack

__all__ = ["ReconstructionEngine", "RenderResult", "Renderer"]

logger = logging.getLogger(__name__)


class Renderer(Protocol):
    """Turns one HTML document into one PDF file."""

    def render(self, html: str, pdf_path: Path) -> None: ...

    def close(self) -> None: ...


RendererFactory = Callable[[], Renderer]


@dataclass
class RenderResult:
    rendered: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    # (encounter_id, exception type name) — never exception text.
    failed: list[tuple[str, str]] = field(default_factory=list)


def _safe_name(value: str | None, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (value or "").strip()).strip("_")
    return cleaned or fallback


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
            "family": _safe_name(record.patient.family_name, "Unknown"),
            "given": _safe_name(record.patient.given_name, "Unknown"),
            "dos": dos.strftime("%m-%d-%Y") if dos else "undated",
            "type": _safe_name(encounter.note_type, "note"),
        }
        return self._pack.manifest.filename.pattern.format(**fields)

    def _allocate_target(
        self, out_dir: Path, name: str, encounter: Encounter, claimed: set[Path]
    ) -> Path:
        """Deterministic name allocation: collisions are resolved against the
        names claimed *this run* (iteration order is stable), so a re-run
        allocates identical names and the idempotent skip works. The loser of
        a same-day collision gets a short source-id suffix — never an
        overwrite."""
        path = out_dir / name
        if path in claimed:
            suffix = encounter.id.replace("-", "")[:8]
            path = path.with_name(f"{path.stem}-{suffix}{path.suffix}")
        claimed.add(path)
        return path

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
                for encounter in record.encounters:
                    self._render_one(encounter, record, template, out, force, claimed, result)
        finally:
            self._retire_renderer()
        return result

    def _render_one(
        self,
        encounter: Encounter,
        record: PatientRecord,
        template: Any,
        out: Path,
        force: bool,
        claimed: set[Path],
        result: RenderResult,
    ) -> None:
        target = self._allocate_target(
            out, self._filename_for(encounter, record), encounter, claimed
        )
        if target.exists() and not force:
            result.skipped.append(target)
            return
        cfg = {
            "sections": self.section_flags,
            "timezone": self._pack.manifest.timezone,
            "tokens": self._pack.manifest.tokens,
        }
        try:
            context = self._pack.build_context(encounter, record, cfg)
            html = template.render(**context)
            self._render_pdf(html, target)
            result.rendered.append(target)
        except Exception as exc:
            logger.error("render failed for encounter %s (%s)", encounter.id, exc_tag(exc))
            result.failed.append((encounter.id, exc_tag(exc)))

    def _render_pdf(self, html: str, target: Path) -> None:
        try:
            self._acquire_renderer().render(html, target)
        except Exception as exc:
            # Crash relaunch: one fresh renderer, one retry, then report.
            logger.warning("renderer crashed (%s); relaunching once", exc_tag(exc))
            self._retire_renderer()
            self._acquire_renderer().render(html, target)
        self._after_render()
