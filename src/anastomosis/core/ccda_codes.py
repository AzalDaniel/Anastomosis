"""The C-CDA vocabulary ``sources/ccda/parser.py`` and
``deliver/ccda_export/builder.py`` must agree on, defined once (84):
neither side may import the other, so these constants live in a leaf
module both depend on rather than being mirrored by hand in two files.
"""

from __future__ import annotations

import re
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from lxml import etree

__all__ = [
    "ARTIFACT_INTEGRITY_ALGORITHM",
    "ARTIFACT_TEMPLATE_ROOT",
    "EXT_PRIOR_LOSS_NARRATIVE",
    "EXT_SECTION_ENTRIES",
    "LOINC_ALLERGIES",
    "LOINC_ENCOUNTERS",
    "LOINC_EXTENSIONS",
    "LOINC_IMMUNIZATIONS",
    "LOINC_MEDICATIONS",
    "LOINC_NOTES",
    "LOINC_PROBLEMS",
    "LOINC_RESULTS",
    "LOINC_SOCIAL",
    "LOINC_VITALS",
    "LOSS_NARRATIVE_GENERATION_ROOT",
    "LOSS_NARRATIVE_TEMPLATE_ROOT",
    "LOSS_NARRATIVE_TITLE",
    "OID_ICD10",
    "OID_RXNORM",
    "OID_SNOMED",
    "OID_SSN",
    "SDTC",
    "SECTION_CODE_UNKNOWN",
    "TPL_SEVERITY",
    "V3",
    "XSI",
    "first_rooted_id",
    "organizer_component_source_id",
]

# XML namespaces.
V3 = "urn:hl7-org:v3"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
SDTC = "urn:hl7-org:sdtc"

# Code-system OIDs both sides key on.
OID_SSN = "2.16.840.1.113883.4.1"
OID_SNOMED = "2.16.840.1.113883.6.96"
OID_ICD10 = "2.16.840.1.113883.6.90"
OID_RXNORM = "2.16.840.1.113883.6.88"

# The allergy Severity Observation template. The builder stamps it on the inner
# observation; the parser keys severity on it and reads displayName off nothing
# else. It lived here as a named constant on the writer's side and a bare string
# on the reader's, which is the one shape the mirror test cannot see — so if the
# two drifted, severity would simply stop coming back and every test would pass.
TPL_SEVERITY = "2.16.840.1.113883.10.20.22.4.8"

# Section LOINC codes: what the builder emits and what the parser dispatches on.
# A section code the parser does not know is captured as foreign narrative, so a
# value that drifts here does not crash — it silently stops being structured.
LOINC_PROBLEMS = "11450-4"
LOINC_ALLERGIES = "48765-2"
LOINC_MEDICATIONS = "10160-0"
LOINC_IMMUNIZATIONS = "11369-6"
LOINC_VITALS = "8716-3"
LOINC_RESULTS = "30954-2"
LOINC_SOCIAL = "29762-2"
LOINC_ENCOUNTERS = "46240-8"
LOINC_NOTES = "34109-9"

# Not structurally parsed. The builder uses 51899-3 as the declared home for
# source fields CDA has no slot for; the parser captures a section STAMPED as
# ours entry-by-entry, and treats any other 51899-3 as a third party's ordinary
# foreign narrative.
LOINC_EXTENSIONS = "51899-3"

# The stamp that makes this tool's loss ledger self-identifying. Recognising it
# is what stops a repeated export -> ingest -> export loop from re-narrating
# generation N-1's whole ledger as one line inside generation N's — an unbounded
# blob that drowned the real entries. Non-OID roots are the exporter's existing
# convention for anastomosis-private identifiers; see docs/CCDA_EXPORT.md for
# why XSD-OID discipline does not apply to this output.
LOSS_NARRATIVE_TEMPLATE_ROOT = "urn:anastomosis:ccda:loss-narrative"
LOSS_NARRATIVE_GENERATION_ROOT = "urn:anastomosis:ccda:loss-narrative:generation"

# The pre-stamp marker: documents exported before the templateId existed carry
# only this title, and the parser still recognises them by it.
LOSS_NARRATIVE_TITLE = "Anastomosis Preserved Source Fields"

# Where a re-ingest parks a stamped section's entries.
EXT_PRIOR_LOSS_NARRATIVE = "ccda:prior_loss_narrative"

# Where a re-ingest parks a section's OWN entries, verbatim:
# ``ccda:entries:<code>``, suffixed ``#2``, ``#3``, … for a repeated section
# code, in document order. Shared because both halves read it now — the parser
# writes the bytes, and the builder re-emits them as entries in the section
# carrying that code rather than narrating them (see docs/CCDA_EXPORT.md).
EXT_SECTION_ENTRIES = "ccda:entries"

# The bucket a section with no ``<code>`` at all parks under: the parser writes
# ``ccda:entries:unknown`` / ``ccda:section:unknown`` for it, the ledger reads
# the same bucket, and the builder re-emits its entries into a section that
# states no code — because the record preserved none, and a code is not a
# detail to invent.
SECTION_CODE_UNKNOWN = "unknown"

# The stamp on a delivered document artifact, and the ED attributes that carry
# its digest. Both halves key on all three: the builder writes an
# <observationMedia> entry per artifact it delivers a sidecar for, and the
# parser reads exactly those entries back into DocumentArtifacts. An unstamped
# <observationMedia> is a third party's multimedia and is left to the ordinary
# narrative/entry capture, which is what it has always been.
#
# Why an entry rather than a second <component><nonXMLBody>: CDA R2 gives a
# ClinicalDocument exactly one <component>, so a CCD carrying a structuredBody
# cannot also carry a nonXMLBody — the shape the C-CDA R2.1 Unstructured
# Document template (2.16.840.1.113883.10.20.22.1.10) is for is a WHOLE
# document, not an attachment to one. <observationMedia> with an ED value is
# base CDA R2's own mechanism for a non-XML artifact inside a structured body,
# and the ED's <reference value="…"/> naming a file beside the document is the
# same construct the reader already resolves for a referenced nonXMLBody.
ARTIFACT_TEMPLATE_ROOT = "urn:anastomosis:ccda:artifact"
#: ED @integrityCheckAlgorithm. "SHA-256" is one of the two values the HL7 v3
#: ED datatype admits, and the digest this toolkit witnesses artifacts with.
ARTIFACT_INTEGRITY_ALGORITHM = "SHA-256"


def first_rooted_id(element: etree._Element) -> tuple[str, str | None] | None:
    """Contract (#378): ``element``'s first direct-child ``<id>`` that
    states a root, or ``None`` — the single reading both round-trip halves
    use. ``nullFlavor="NI"`` states no id and is skipped, not read as an
    empty root; a root blank after ``.strip()`` does not stop the search.
    "First rooted id", not "first id"."""
    for id_node in element.findall(f"{{{V3}}}id"):
        if id_node.get("nullFlavor") is not None:
            continue
        root = id_node.get("root")
        root = root.strip() if root is not None else ""
        if not root:
            continue
        extension = id_node.get("extension")
        extension = extension.strip() if extension is not None else None
        return root, extension or None
    return None


# A GUID-shaped string: the synthetic-fixture prefix OR the canonical 8-4-4-4-12
# hex form a real EHR would emit. Either is already globally unique, so it needs
# no assigning authority to disambiguate it and is trusted verbatim.
GUID_RE = re.compile(
    r"^(?:feedface-|00000000-)[0-9a-fA-F-]+$|"
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    re.IGNORECASE,
)


def identity_from_ii(
    kind: str,
    id_pair: tuple[str, str | None] | None,
    fallback: str,
    *,
    bare_root_names_the_instance: bool,
) -> str:
    """Contract (7, #404, #412): the one place an HL7 v3 ``II``
    ``(root, extension)`` becomes a canonical id. An extension paired with
    a root hashes both; a GUID root alone is trusted verbatim; a non-GUID
    root alone names the instance only when
    ``bare_root_names_the_instance``; no usable id takes ``fallback``."""
    if id_pair is not None:
        root, extension = id_pair
        if extension:
            name = f"anastomosis:ccda:{kind}:{quote(root, safe='')}:{quote(extension, safe='')}"
            return str(uuid5(NAMESPACE_URL, name))
        if GUID_RE.match(root):
            return root
        if bare_root_names_the_instance:
            return str(uuid5(NAMESPACE_URL, f"anastomosis:ccda:{kind}:{quote(root, safe='')}"))
    return str(uuid5(NAMESPACE_URL, f"anastomosis:ccda:{fallback}"))


def organizer_component_source_id(root: str, extension: str | None, index: int) -> str:
    """Contract: a provenance id for an organizer component that states
    none of its own, derived once here so parser and builder always agree
    on the same id for the same position. Document-intrinsic (no
    ``source_file``): survives an export/re-ingest round trip under a
    different filename. ``index`` is the 0-based component position."""
    name = (
        f"anastomosis:ccda:organizer:{quote(root, safe='')}:"
        f"{quote(extension or '', safe='')}:component:{index}"
    )
    return str(uuid5(NAMESPACE_URL, name))
