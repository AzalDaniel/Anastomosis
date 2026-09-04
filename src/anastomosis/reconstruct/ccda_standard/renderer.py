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

    ``records`` is the record that produced each entry of ``documents``, one per
    index — additive, so the two existing readers of ``documents``
    (``cli_commands/migrate.py`` and ``core/migrate.py``, both counting or
    truthiness-checking it) are untouched. A caller that needs to grade what was
    actually written — the pipeline's QA stage — needs the record BESIDE the
    path, because ``documents`` alone cannot say whose bytes are at a path two
    patients' records mapped to (see ``_allocate``: same ``patient.id``, same
    file). Kept parallel rather than zipped into ``documents`` itself so the
    existing field stays exactly what it always was.

    ``by_path`` is the SAME association, deduplicated: one entry per distinct
    rendered path, naming the record whose render actually WROTE the bytes
    there (an idempotent skip never overwrites an entry a write already
    claimed this batch — see ``render_ccda_standard``). ``records`` is derived
    from it after the batch completes, so both fields always agree and a
    caller re-zipping ``documents``/``records`` itself (#383's round-two
    blocker: a plain ``dict(zip(...))`` keeps the LAST list entry, which under
    ``force=False`` is whichever record's render took the idempotent-skip
    branch — never the writer) would now get the same answer either way. A
    caller grading what was written should read ``by_path`` directly rather
    than re-deriving it.
    """

    documents: list[Path] = field(default_factory=list)
    records: list[PatientRecord] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    by_path: dict[Path, PatientRecord] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A caller building one of these directly (this dataclass is public)
        # with ``documents`` but no matching ``records`` used to fail far from
        # the mistake — a `zip(..., strict=True)` ``ValueError`` at whatever
        # call site happened to pair them next. Naming both fields here says
        # what went wrong where it went wrong.
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
    family = safe_name(patient.family_name, "Unknown")
    given = safe_name(patient.given_name, "Unknown")
    return out_dir / f"{family}_{given}_{digest}_ccda.pdf"


def ccda_standard_doc_path(out_dir: str | Path, record: PatientRecord) -> Path:
    """The deterministic per-patient PDF path this view renders for ``record``.

    Public alias of the internal allocator so a caller (e.g. the migration's
    upload-manifest writer) can recover the path:patient association from the
    records WITHOUT re-implementing the naming rule — the whole-patient view has
    no :class:`~anastomosis.reconstruct.engine.RenderedDoc` list of its own.
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

    Atomic writes and an idempotent skip (an existing PDF is kept unless
    ``force``) match the per-encounter engine's safety. A per-patient failure is
    recorded as ``(patient_id, exception-type)`` and never aborts the batch.
    ``renderer_factory`` is injectable for tests (a fake Chromium); it defaults
    to the real Chromium renderer, constructed lazily so a no-render batch (all
    skipped) needs no browser.

    ``result.by_path`` is kept updated as the batch runs: a WRITE always
    claims (or reclaims) its path — those are the bytes now on disk — while an
    idempotent SKIP only ``setdefault``s it, never displacing a record this
    same batch already established as the writer. Two ``PatientRecord``s
    sharing one ``patient.id`` (``_allocate`` keys on it) render to the SAME
    path; under ``force=False`` the first to run WRITES and every later one
    SKIPS, so without this rule the association would be whichever record ran
    LAST — the skip, not the writer (#383's round-two blocker: a caller
    re-zipping ``documents``/``records`` with a plain ``dict(zip(...))`` gets
    exactly that wrong last-entry-wins answer). ``records`` is then derived
    from the finished ``by_path`` so both fields describe the writer even for
    an entry appended before the batch settled which record that was.
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
