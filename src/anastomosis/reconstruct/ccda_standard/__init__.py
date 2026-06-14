"""Standard C-CDA render path — a neutral HL7 view of the delivered payload.

Renders the C-CDA a migration actually moves (via the vendored HL7 ``CDA.xsl``)
into a human-readable PDF, so cross-EHR output is neutral rather than skinned to
the source vendor. See :mod:`.renderer` for the pipeline and ``vendor/PINNED.md``
for the pinned, checksum-verified stylesheet.
"""

from __future__ import annotations

from .renderer import CCDARenderResult, render_ccda_html, render_ccda_standard

__all__ = ["CCDARenderResult", "render_ccda_html", "render_ccda_standard"]
