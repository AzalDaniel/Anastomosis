"""Anastomosis: reconstruct, verify, and re-home clinical records.

An anastomosis is the surgical connection between two structures — this
toolkit is that connection for EHR exports: it parses raw EHI, rebuilds
human-readable charts, verifies every byte against the source, and delivers
the result to a new EHR, a FHIR endpoint, or a searchable offline archive.

Local-first: the core pipeline makes no network calls.
"""

__version__ = "0.7.0"
