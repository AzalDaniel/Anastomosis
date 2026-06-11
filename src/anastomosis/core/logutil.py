"""Logging with PHI redaction (security backlog: log redaction, from M1).

The discipline is "never log patient names, DOBs, or identifiers" — but
discipline fails, so this module is the defense-in-depth behind it:

* :func:`redact` scrubs SSN/phone/email/date shapes from any string.
* :class:`RedactionFilter` applies :func:`redact` to every record that
  passes through a handler, including interpolated args and exception text.
* :func:`exc_tag` is what error paths log instead of ``str(e)`` — exception
  *messages* frequently embed the input that caused them (a patient name in
  a parse error), while the exception *type* is always safe.

Adapters and the pipeline must log **counts, table names, and opaque ids**,
never field values. The filter exists for the day someone forgets.
"""

from __future__ import annotations

import logging
import re

__all__ = ["RedactionFilter", "configure_logging", "exc_tag", "redact"]

# Shapes that are PHI wherever they appear in a log line. Dates are included
# deliberately: a date inside a log *message* is almost always input-derived
# (a DOB or date of service) — the timestamp belongs to the formatter.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED-SSN]"),
    (re.compile(r"\(\d{3}\)\s*\d{3}-\d{4}|\b\d{3}[-.]\d{3}[-.]\d{4}\b"), "[REDACTED-PHONE]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[REDACTED-EMAIL]"),
    (re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b"), "[REDACTED-DATE]"),
)


def redact(text: str) -> str:
    """Scrub PHI-shaped substrings from ``text``."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def exc_tag(exc: BaseException) -> str:
    """A loggable name for an exception whose message may embed input."""
    return type(exc).__name__


class RedactionFilter(logging.Filter):
    """Redact PHI shapes from every record this filter sees.

    Interpolation happens here (``record.getMessage()``) so values passed as
    args are scrubbed too; exception text is folded in and scrubbed rather
    than letting the formatter render the raw traceback message.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            message = f"{message} [{exc_tag(record.exc_info[1])}]"
            record.exc_info = None
            record.exc_text = None
        record.msg = redact(message)
        record.args = ()
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """Set up root logging with redaction installed on the handler."""
    handler = logging.StreamHandler()
    handler.addFilter(RedactionFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
