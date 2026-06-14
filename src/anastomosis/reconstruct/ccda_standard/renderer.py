"""The standard C-CDA render path: a neutral, vendor-faithful view of the payload.

Real EHR-to-EHR migrations move structured C-CDA/FHIR; the rendered PDF is the
human-readable archive of that payload. This path renders the **actual C-CDA**
the target imports — via HL7's own stylesheet — so an Athena→Epic migration
produces a neutral standard view, never a Practice Fusion-styled note (PF is one
opt-in Jinja skin, not a privileged default).

Pipeline (per patient):

    record → deliver.ccda_export.build_ccd  (deterministic C-CDA bytes)
           → HL7 CDA.xsl                     (vendored, XSLT 1.0, read_network=False)
           → XHTML
           → reconstruct.chromium.ChromiumRenderer
           → one {family}_{given}_ccda.pdf

The stylesheet is the unmodified upstream HL7 ``CDA.xsl`` vendored under
``vendor/`` (see ``vendor/PINNED.md`` for the pinned tag and checksums). It is
XSLT 1.0, so ``lxml``/``libxslt`` runs it natively. The transform runs under
:class:`lxml.etree.XSLTAccessControl` with ``read_network=False`` — the
stylesheet's local ``document()`` companions (l10n, narrative-block whitelist)
resolve off the file base URI, but no remote fetch can occur (the no-egress
invariant). The stylesheet's own ``limit-pdf`` / ``limit-external-images``
sandbox parameters are left at their secure ``'yes'`` defaults.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from lxml import etree

from anastomosis.core.logutil import exc_tag
from anastomosis.core.output import secure_output_dir
from anastomosis.deliver.ccda_export.builder import build_ccd

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from anastomosis.core.model import PatientRecord
    from anastomosis.reconstruct.engine import Renderer

__all__ = ["CCDARenderResult", "render_ccda_html", "render_ccda_standard"]

logger = logging.getLogger(__name__)

# The vendored HL7 stylesheet (unmodified; see vendor/PINNED.md).
CDA_XSL = Path(__file__).resolve().parent / "vendor" / "CDA.xsl"

# Hardened parser for the (public) C-CDA input: no external network, no entity
# resolution — defense-in-depth against XXE / entity-expansion even though the
# pipeline's own build_ccd output is entity-free and trusted.
_INPUT_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)


@lru_cache(maxsize=1)
def _transform() -> etree.XSLT:
    """The compiled HL7 ``CDA.xsl``, loaded once and reused.

    Parsing the stylesheet from its file path sets the base URI so its
    ``document('cda_l10n.xml')`` / ``document('cda_narrativeblock.xml')`` calls
    resolve to the co-located vendored companions. ``read_network=False`` blocks
    any remote ``document()`` egress while keeping local file reads enabled.

    The single cached ``XSLT`` object is reused across calls; libxslt transforms
    are not guaranteed reentrant, so the callers must be sequential (today: the
    CLI is single-threaded and the GUI runs behind its busy-guard). A future
    concurrent caller must serialize the transform or hold it per-thread.
    """
    tree = etree.parse(str(CDA_XSL))
    # lxml-stubs declares XSLTAccessControl with no __init__, so mypy rejects its
    # runtime-valid kwargs; read_network=False is the no-egress guarantee.
    access = etree.XSLTAccessControl(read_network=False, read_file=True)  # type: ignore[call-arg]
    return etree.XSLT(tree, access_control=access)


def render_ccda_html(ccd_bytes: bytes) -> str:
    """Transform a C-CDA document (``build_ccd`` output) to neutral XHTML.

    Deterministic: ``build_ccd`` is byte-stable and the XSLT is pure, so the same
    record yields the same XHTML. The output is HL7's standard view of the
    structured payload — it carries no Practice Fusion (or any vendor) skin.
    """
    result = _transform()(etree.fromstring(ccd_bytes, parser=_INPUT_PARSER))
    return str(result)


@dataclass
class CCDARenderResult:
    """What a standard-C-CDA-view batch produced (presentation-free, PHI-safe).

    ``documents`` are the written/kept PDFs (one per patient); ``failed`` carries
    ``(patient_id, exception-type-name)`` pairs — pseudonymous ids and type names
    only, never exception text — mirroring the engine's PHI-safe failure record.
    """

    documents: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def _safe(value: str | None, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (value or "").strip()).strip("_")
    return cleaned or fallback


def _default_renderer() -> Renderer:
    from anastomosis.reconstruct.chromium import ChromiumRenderer

    return ChromiumRenderer(page_size="Letter")


def _allocate(out_dir: Path, record: PatientRecord) -> Path:
    """The deterministic per-patient output path.

    The filename embeds a stable hash of the patient id, so it uniquely
    identifies the patient: two patients sharing family+given never collide —
    within one batch OR across batches into the same dir — and the idempotent
    skip is therefore sound (a re-run of the SAME patient maps to the SAME file;
    a different patient never maps onto an existing one, so no silent drop or
    mis-attribution). The per-encounter engine keys on family+given+DOS instead;
    this whole-patient view has no encounter date, so identity rides the id hash.
    """
    patient = record.patient
    digest = hashlib.sha256(patient.id.encode("utf-8")).hexdigest()[:12]
    family = _safe(patient.family_name, "Unknown")
    given = _safe(patient.given_name, "Unknown")
    return out_dir / f"{family}_{given}_{digest}_ccda.pdf"


def _write_pdf(renderer: Renderer, html: str, target: Path) -> None:
    """Render to a sibling temp file then atomically replace (no partial PDF)."""
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        renderer.render(html, tmp)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def render_ccda_standard(
    records: Iterable[PatientRecord],
    out_dir: str | Path,
    *,
    force: bool = False,
    renderer_factory: Callable[[], Renderer] | None = None,
) -> CCDARenderResult:
    """Render one standard-C-CDA-view PDF per patient into ``out_dir``.

    Atomic writes and an idempotent skip (an existing PDF is kept unless
    ``force``) match the per-encounter engine's safety. A per-patient failure is
    recorded as ``(patient_id, exception-type)`` and never aborts the batch.
    ``renderer_factory`` is injectable for tests (a fake Chromium); it defaults
    to the real Chromium renderer, constructed lazily so a no-render batch (all
    skipped) needs no browser.
    """
    out = secure_output_dir(out_dir)
    factory = renderer_factory or _default_renderer
    result = CCDARenderResult()
    renderer: Renderer | None = None
    try:
        for record in records:
            target = _allocate(out, record)
            if target.exists() and not force:
                result.skipped.append(target)
                result.documents.append(target)
                continue
            try:
                html = render_ccda_html(build_ccd(record))
                if renderer is None:
                    renderer = factory()
                _write_pdf(renderer, html, target)
                result.documents.append(target)
            except Exception as exc:
                logger.error(
                    "ccda_standard render failed for patient %s (%s)",
                    record.patient.id,
                    exc_tag(exc),
                )
                result.failed.append((record.patient.id, exc_tag(exc)))
    finally:
        if renderer is not None:
            renderer.close()
    return result
