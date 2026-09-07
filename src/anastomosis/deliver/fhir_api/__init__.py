"""FHIR R4 API delivery (PLAN item 13a): the network alternate to the browser route.

:mod:`.client` — :class:`FhirEndpoint`/:class:`FhirClient`, a stdlib
``urllib`` REST client; no ``fhir.resources`` at runtime (tests only).
:mod:`.destination` — :class:`FhirApiDestination`, resolves patients and
posts ``DocumentReference``. :mod:`.attach` — ``attach_fhir_destination``,
the seam ``anast upload --fhir`` calls.

Nothing is re-exported here: cheap imports (RULES.md 75).
"""

from __future__ import annotations
