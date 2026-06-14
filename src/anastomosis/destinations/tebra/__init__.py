"""The Tebra (Kareo) browser destination pack — a FLOW SHAPE, not selectors.

Tebra (formerly Kareo + PatientPop) publishes NO document-write API (the
capability registry records that negative claim with evidence), so filing a
reconstructed chart into Tebra means driving its web UI — a browser pack.

This pack ships as the *shape* of that flow only: ``pack.yaml`` declares every
selector slot at the ``DISCOVER`` placeholder. We have NO access to a live Tebra
instance, so inventing a single CSS selector, URL path, or DOM detail would be a
no-hallucination-rule violation. The selectors are OPERATOR-DERIVED, per
practice, via ``anast destination init tebra`` — and Tebra rotating its UI is a
one-pack event the operator re-discovers, never a code change here.

The pack is data: there is no Python flow logic here. The generic
:class:`anastomosis.destinations.browserpack.BrowserPackDestination` drives the
discovered selectors; this package exists only so the scaffold ships in the
wheel (like ``packs/generic_soap``).
"""

from __future__ import annotations
