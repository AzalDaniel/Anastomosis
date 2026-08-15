"""API delivery: file reconstructed charts into a FHIR R4 server (PLAN item 13a).

The modern counterpart to the browser route. When a destination speaks FHIR R4,
a chart's notes are filed as ``DocumentReference`` resources over HTTPS rather
than driven through a web UI — the same upload engine, tracking ledger, and
L0-L6 verifier drive it unchanged, because :class:`FhirApiDestination`
implements the same :class:`~anastomosis.destinations.base.Destination`
protocol (plus the optional ``MetadataReader``/``DocumentReader`` capabilities
that give the verifier L5/L6).

* :mod:`.client` — :class:`FhirEndpoint` (loopback-only http exception, masked
  token) + :class:`FhirClient`, a minimal JSON REST client on stdlib
  ``urllib`` with status->delivery-error routing. No new dependencies.
* :mod:`.destination` — :class:`FhirApiDestination`, the pusher: identifier-based
  patient resolution (reusing the export identifier systems), a re-read banner
  defense, a title-fingerprint duplicate scan, and the DocumentReference driver.

The runtime modules work WITHOUT ``fhir.resources`` installed — resources are
built as plain stdlib dicts. The ``fhir`` extra is used only by the tests, to
validate the constructed DocumentReference against the real R4 schema.
"""

from __future__ import annotations

from .client import FhirClient, FhirEndpoint
from .destination import FhirApiDestination

__all__ = ["FhirApiDestination", "FhirClient", "FhirEndpoint"]
