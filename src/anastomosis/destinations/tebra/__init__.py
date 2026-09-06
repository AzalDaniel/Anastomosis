"""The Tebra (Kareo) browser destination pack — a FLOW SHAPE, not selectors.

Tebra publishes no document-write API, so filing means driving its web UI.
``pack.yaml`` ships every selector slot at ``DISCOVER`` (RULES.md 27) —
operator-derived via ``anast destination init tebra``, never invented here.
No Python flow logic: the generic ``BrowserPackDestination`` drives them;
this package only ships the scaffold.
"""

from __future__ import annotations
