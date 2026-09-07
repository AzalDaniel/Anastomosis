"""The standard C-CDA render path: a neutral, vendor-faithful view of the payload.

Renders the actual C-CDA the target imports, via HL7's own ``CDA.xsl``
(vendored, pinned — see ``vendor/PINNED.md``), never a vendor-styled skin.

Pipeline: record → ``deliver.ccda_export.build_ccd`` → ``CDA.xsl`` (XSLT 1.0)
→ XHTML → :class:`~anastomosis.reconstruct.chromium.ChromiumRenderer` → PDF.
Runs under :class:`lxml.etree.XSLTAccessControl` with ``read_network=False``:
local ``document()`` companions resolve, no remote fetch can occur.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from lxml import etree

from anastomosis.core.atomic import atomic_replace
from anastomosis.core.logutil import exc_tag, safe_log_id
from anastomosis.core.output import secure_output_dir
from anastomosis.core.textutil import safe_name
from anastomosis.deliver.ccda_export.builder import build_ccd

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from anastomosis.core.model import PatientRecord
    from anastomosis.reconstruct.engine import Renderer

__all__ = [
    "CCDARenderResult",
    "ccda_standard_doc_path",
    "render_ccda_html",
    "render_ccda_standard",
]

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

    Parsed from its file path so ``document()`` calls resolve to the
    co-located vendored companions; ``read_network=False`` blocks remote
    egress while local reads stay enabled. Cached and reused: libxslt
    transforms are not reentrant, so callers must be sequential.
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

    Contract: ``documents``/``records`` stay parallel (index i's record wrote
    or already occupied ``documents[i]``); ``failed`` is
    ``(patient_id, exception-type)`` pairs only. ``by_path`` is the same
    association deduplicated by path — the writer, never a later idempotent
    skip — and is the one to read directly; ``records`` is derived from it
    after the batch so the two never disagree (#383).
    """

    documents: list[Path] = field(default_factory=list)
    records: list[PatientRecord] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    by_path: dict[Path, PatientRecord] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A caller building one of these directly with mismatched documents
        # and records would otherwise fail far from the mistake — inside
        # whatever call site next tried to zip them.
        if len(self.documents) != len(self.records):
            raise ValueError(
                "CCDARenderResult.documents and .records must stay parallel "
                f"(got {len(self.documents)} documents, {len(self.records)} records)"
            )


def _default_renderer() -> Renderer:
    from anastomosis.reconstruct.chromium import ChromiumRenderer

    return ChromiumRenderer(page_size="Letter")


def _allocate(out_dir: Path, record: PatientRecord) -> Path:
    """The deterministic per-patient output path.

    The filename embeds a hash of the patient id, so identity rides the id
    hash rather than family+given+DOS (this whole-patient view has no
    encounter date): two patients sharing a name never collide, and a
    re-run of the same patient maps to the same file.
    """
    patient = record.patient
    digest = hashlib.sha256(patient.id.encode("utf-8")).hexdigest()[:12]
    family = safe_name(patient.family_name, "Unknown")
    given = safe_name(patient.given_name, "Unknown")
    return out_dir / f"{family}_{given}_{digest}_ccda.pdf"


def ccda_standard_doc_path(out_dir: str | Path, record: PatientRecord) -> Path:
    """The deterministic per-patient PDF path this view renders for ``record``.

    Public alias of the internal allocator, so a caller can recover the
    path:patient association without re-implementing the naming rule.
    """
    return _allocate(Path(out_dir), record)


def _write_pdf(renderer: Renderer, html: str, target: Path) -> None:
    """Render to a sibling temp file then atomically replace (no partial PDF)."""
    with atomic_replace(target) as tmp:
        renderer.render(html, tmp)


def render_ccda_standard(
    records: Iterable[PatientRecord],
    out_dir: str | Path,
    *,
    force: bool = False,
    renderer_factory: Callable[[], Renderer] | None = None,
) -> CCDARenderResult:
    """Render one standard-C-CDA-view PDF per patient into ``out_dir``.

    Atomic writes and an idempotent skip (existing PDF kept unless
    ``force``); a per-patient failure is recorded, never aborts the batch.
    ``renderer_factory`` defaults to the real Chromium renderer, built
    lazily so an all-skipped batch needs no browser.

    ``result.by_path`` tracks the WRITER per path (see
    :class:`CCDARenderResult`): a write always claims it, a skip only
    ``setdefault``s it, so two records sharing one ``patient.id`` resolve to
    the record that actually wrote, not whichever ran last (#383).
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
                result.by_path.setdefault(target, record)
                continue
            try:
                html = render_ccda_html(build_ccd(record))
                if renderer is None:
                    renderer = factory()
                _write_pdf(renderer, html, target)
                result.documents.append(target)
                result.by_path[target] = record
            except Exception as exc:
                logger.error(
                    "ccda_standard render failed for patient %s (%s)",
                    safe_log_id(record.patient.id),
                    exc_tag(exc),
                )
                result.failed.append((record.patient.id, exc_tag(exc)))
    finally:
        if renderer is not None:
            renderer.close()
    result.records = [result.by_path[doc] for doc in result.documents]
    return result
