"""The C-CDA vocabulary the reader and the writer must agree on, defined once.

``sources/ccda/parser.py`` and ``deliver/ccda_export/builder.py`` are the two
halves of one round trip, and their contract is that the builder emits exactly
what the parser traverses. That made twenty-one constants obligate to mirror —
and they were mirrored by hand, in two files, with a comment on each side
telling the reader so.

The comment was wrong. It said "these four must mirror
sources/ccda/parser.py exactly" over a block of five, one of which
(``LOSS_NARRATIVE_TEMPLATE_VERSION``) the parser has never had, while sixteen
others mirrored silently a few lines above with nothing saying they had to. A
value that must agree across a boundary should not be a promise a reader has
to keep; it should be one definition.

Neither side may import the other — ``sources`` and ``deliver`` are
deliberately orthogonal, and there is a test that says so — so the definitions
live here, in a leaf module of literals that both may depend on.
"""

from __future__ import annotations

__all__ = [
    "ARTIFACT_INTEGRITY_ALGORITHM",
    "ARTIFACT_TEMPLATE_ROOT",
    "EXT_PRIOR_LOSS_NARRATIVE",
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
    "TPL_SEVERITY",
    "V3",
    "XSI",
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
