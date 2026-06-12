"""C-CDA export deliverer — the inverse of :mod:`anastomosis.sources.ccda`.

Generates HL7 C-CDA R2.1 / CCD XML from canonical :class:`PatientRecord`
objects, for destinations that import C-CDA (the router's middle route between
a native write API and browser automation). The single hard contract is the
round trip: ``parse(build_ccd(record)) ≈ record`` through this repo's own
C-CDA parser. See :mod:`.builder` for scope, determinism, and the declared
list of source fields that do not survive a C-CDA round trip.
"""

from .builder import DECLARED_LOSSES, build_ccd
from .deliverer import deliver_ccda

__all__ = ["DECLARED_LOSSES", "build_ccd", "deliver_ccda"]
