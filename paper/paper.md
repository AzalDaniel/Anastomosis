---
title: 'Anastomosis: reconstruct, verify, and re-home electronic health records'
tags:
  - Python
  - electronic health records
  - health informatics
  - interoperability
  - FHIR
  - C-CDA
  - data migration
  - clinical documents
authors:
  # TODO(author): add your ORCID (https://orcid.org) before submission, e.g.
  #   orcid: 0000-0000-0000-0000
  - name: Azal Daniel
    corresponding: true
    affiliation: 1
affiliations:
  # TODO(author): JOSS expects "Name, Country" — add your country before
  # submission, and replace with an institutional affiliation if applicable.
  - name: Independent Researcher
    index: 1
# TODO(author): set to the actual submission date.
date: 11 June 2026
bibliography: paper.bib
---

# Summary

When a medical practice leaves an electronic health record (EHR) system, it
receives its own data back as a pile of vendor-native database tables — and
the clinical narrative, the part of the chart clinicians actually read,
frequently does not survive the move to the next system. Anastomosis is an
open-source toolkit that closes that gap. It ingests raw EHR exports
(Practice Fusion/Tebra EHI table dumps and HL7 C-CDA documents [@ccdaR21] in
v0.1.0), maps them into a lossless canonical model aligned with HL7 FHIR R4
[@fhirR4], reconstructs human-readable encounter documents through templated
browser-based rendering, verifies every rendered document with an automated
quality-assurance engine, and delivers the result as a searchable offline
archive, per-patient record-request bundles, and FHIR R4 bundles. The
pipeline is local-first by design — it makes no network calls — so protected
health information never leaves the operator's machine. Anastomosis is
written in Python (3.11+), strictly typed, tested on Linux and Windows, and
distributed under AGPL-3.0 with a single-command CLI (`anast pipeline run`).

# Statement of need

Under the 21st Century Cures Act final rule, every certified EHR in the
United States must let a practice export its complete electronic health
information (45 CFR §170.315(b)(10)) [@onc2020cures]. The rule mandates that
the data come out; it does not mandate a usable format. In practice, vendors
satisfy it with native table dumps — tab-separated files mirroring internal
schemas, with notes spread as markup fragments across joined tables — that no
other system ingests directly. A systematic review of EHR-to-EHR transitions
identifies data migration and continuity of the legacy record as persistent,
under-addressed risk areas [@miakelye2023].

The burden lands on clinicians, who already spend close to two hours on EHR
and desk work for every hour of direct clinical time [@sinsky2016], with
primary-care physicians spending more than half of their workday inside the
EHR [@arndt2017]; clerical burden of this kind is associated with physician
burnout [@shanafelt2016]. A practice that switches systems faces a stark
choice: pay for commercial migration or archival services, manually re-enter
years of notes into the new system, or lose convenient access to the legacy
record — even though state laws require records to be retained for years
after the move (statutory minimums vary by state, predominantly five to ten
years, longer for pediatric records) [@oncretention].

Anastomosis serves three audiences. Practices migrating between EHRs get
faithful, reconstructed charts ready for import into the destination system.
Practices that have already left a vendor get a self-contained offline
archive — plain folders, PDFs, HTML, and JSON, readable without any
database — instead of an indefinite legacy-EHR or archive-vendor
subscription. And health-informatics researchers get an instrument: lossless,
inspectable parsers for vendor EHI exports make questions about export
completeness and clinical-note portability empirically testable.

# State of the field

Open-source health-data tooling concentrates at the format-conversion layer.
Microsoft's FHIR-Converter [@fhirconverter] translates HL7v2, C-CDA, and JSON
into FHIR; cda2fhir [@cda2fhir] transforms C-CDA documents into FHIR
resources; interface engines route live HL7 messages between running systems;
and Synthea [@walonoski2018] generates synthetic patient records for testing.
To the author's knowledge, no open-source tool addresses the
migration-specific stages: parsing vendor-native EHI exports (which the
format converters do not read), reconstructing human-readable encounter
documents from raw tables, or verified delivery into a destination system
that lacks an import API. Anastomosis fills exactly these stages and
deliberately interoperates with — rather than reimplements — the converter
ecosystem: its canonical model exports to and re-ingests from standard FHIR
R4 bundles with exact round-trips, so existing FHIR tooling composes with it.
A new package, rather than an extension of an existing converter, was the
right shape because the gap is architectural: converters map between document
formats, whereas Anastomosis owns the end-to-end pipeline from undocumented
vendor tables to verified, delivered documents.

# Software design

Anastomosis is a five-stage pipeline — ingest → canonical model →
reconstruct → verify → deliver — governed by four invariants.

**Losslessness.** No source field is ever silently dropped. Every model
object carries a namespaced `extensions` mapping where unmapped vendor
columns ride along; adapters declare the columns they consume; FHIR
export/ingest round-trips are proven exact by tests.

**Loud failure.** Unknown formats raise immediately. Vendor sentinel values
(`\N`, `-1`, `1/1/0001`) map to `None`, never to fabricated values — a
placeholder that can collide with a real clinical value is treated as a
defect class of its own.

**Module isolation.** Source adapters, document template packs, QA checks,
and delivery destinations are versioned modules behind defensive registries:
a broken or outdated pack is diagnosed and disabled without affecting the
rest of the system, so a vendor changing its export format is a one-module
event, never a system event.

**Verification.** Every rendered document is checked by a QA engine —
placeholder and unresolved-template leak detection, layout and pagination,
LOINC-coded vitals presence, and date-staleness checks — using
boundary-anchored matching, because naive substring matching demonstrably
false-passes missing content. The pipeline exits nonzero if any document
fails.

Because the subject matter is protected health information, the engineering
process itself is PHI-safe: the repository contains only synthetic fixtures;
a hashed deny-list scanner runs at pre-commit and in CI; logging records
counts, identifiers, and exception types — never values; output directories
are created with owner-only permissions. The codebase is `mypy --strict`
clean behind security-focused lint gates, and CI runs the full suite on
Linux and Windows.

# Research impact statement

Anastomosis generalizes a private production system, built by the same
author, that reconstructed 12,906 encounter documents from one clinic's
vendor EHI export at a 100% final QA pass rate and uploaded them into the
destination EHR with zero wrong-patient events — collapsing an estimated
five months of manual re-entry into hours. Version 0.1.0 releases the first
generalized vertical slice of that system (ingest → reconstruct → verify →
archive) as installable, citable open source.

Its near-term significance is twofold. For practices — particularly the
small and solo practices least able to afford commercial migration
services — it is a free, auditable escape from vendor lock-in and
legacy-archive fees. For research, it provides a missing instrument for
studying EHI-export quality empirically: because ingestion is lossless and
inspectable, differences between what a vendor exports and what the chart
contained become measurable, which bears directly on information-blocking
policy evaluation [@onc2020cures] and on the transition risks documented in
the literature [@miakelye2023]. The public roadmap includes verified
browser-automation delivery with multi-layer wrong-patient defenses,
additional vendor adapters, and a template-pack learner that derives document
layouts from sample PDFs.

# AI usage disclosure

Anastomosis is developed with substantial assistance from AI coding agents
(Anthropic Claude-family models) operating under the author's direction and
review: the author sets the scope, architecture, clinical-domain
requirements, and acceptance criteria; agent-produced code is adversarially
reviewed and must pass strict static-analysis, test, and PHI-scanning gates
before merge; design decisions of record are documented in the repository.
The predecessor production system was likewise built by the author with AI
assistance and was validated against real-world data by the operating
clinic. This paper was drafted with AI assistance and reviewed and edited by
the author.

# Acknowledgements

The author thanks the clinic whose real-world migration need motivated the
predecessor system.

# References
