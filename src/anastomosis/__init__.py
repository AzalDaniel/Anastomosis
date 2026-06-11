"""Anastomosis: reconstruct, verify, and re-home clinical records.

An anastomosis is the surgical connection between two structures.
This toolkit is that connection for electronic health records:
it parses raw EHI exports, rebuilds human-readable charts, verifies
every byte against the source, and delivers the result wherever it
needs to live next — a new EHR, a FHIR endpoint, or a searchable
offline archive.

Local-first by design: the core pipeline makes no network calls.
"""

__version__ = "0.1.0.dev0"
