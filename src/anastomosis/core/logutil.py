"""PHI redaction as a defense-in-depth behind the logging discipline (2):
:func:`redact` scrubs SSN/phone/email/date shapes; :class:`RedactionFilter`
applies it to every record a handler sees; :func:`exc_tag` is what error
paths log instead of ``str(exc)`` (2); :func:`safe_log_id` is a run-scoped
HMAC surrogate for a source-derived identifier."""

from __future__ import annotations

import hmac
import logging
import re
import secrets

__all__ = ["RedactionFilter", "configure_logging", "exc_tag", "redact", "safe_log_id"]

# Shapes that are PHI wherever they appear in a log line. Dates are included
# deliberately: a date inside a log *message* is almost always input-derived
# (a DOB or date of service) — the timestamp belongs to the formatter. The
# date shapes cover M/D/Y slashes, ISO YYYY-M-D, and D-M-YYYY dashes (padded
# or not; the padded MM-DD-YYYY form is what rendered chart filenames use).
# The SSN (3-2-4) and phone (3-3-4) patterns run first, so the wider date
# runs cannot swallow them.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED-SSN]"),
    (re.compile(r"\(\d{3}\)\s*\d{3}-\d{4}|\b\d{3}[-.]\d{3}[-.]\d{4}\b"), "[REDACTED-PHONE]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[REDACTED-EMAIL]"),
    (
        re.compile(
            r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}-\d{1,2}-\d{4}\b"
        ),
        "[REDACTED-DATE]",
    ),
)


def redact(text: str) -> str:
    """Scrub PHI-shaped substrings from ``text``."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def exc_tag(exc: BaseException) -> str:
    """A loggable name for an exception whose message may embed input."""
    return type(exc).__name__


_LOG_ID_KEY = secrets.token_bytes(32)


def safe_log_id(value: object) -> str:
    """A loggable, run-scoped HMAC-SHA256 surrogate for a source-derived
    identifier: correlates within a run, unlinkable across runs, unconfirmable
    without this run's ephemeral key. The 12-hex truncation trades collision
    headroom for readability; a collision costs correlation quality, never
    data — real identifiers live only in the ledger and reports."""
    if value is None or value == "":
        return "id:unknown"
    digest = hmac.new(_LOG_ID_KEY, str(value).encode("utf-8"), "sha256").hexdigest()[:12]
    return f"id:{digest}"


class RedactionFilter(logging.Filter):
    """Redact PHI shapes from every record this filter sees. Interpolation
    happens here (``record.getMessage()``) so args are scrubbed too;
    exception text is folded in and scrubbed rather than left for the
    formatter to render raw."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            message = f"{message} [{exc_tag(record.exc_info[1])}]"
            record.exc_info = None
            record.exc_text = None
        record.msg = redact(message)
        record.args = ()
        return True


def _has_redacting_handler(logger: logging.Logger) -> bool:
    """True when ``logger`` already carries a handler with a RedactionFilter."""
    return any(
        any(isinstance(f, RedactionFilter) for f in handler.filters) for handler in logger.handlers
    )


def configure_logging(level: int = logging.WARNING) -> None:
    """Contract: after this returns, every root-level handler carries
    :class:`RedactionFilter` — a host's pre-existing handler (a
    ``logging.basicConfig``, a test caplog) is joined in place, since a new
    handler beside it would leave it emitting unredacted. Idempotent: a
    second call sweeps late arrivals only, never stacks a second handler."""
    root = logging.getLogger()
    already_configured = _has_redacting_handler(root)
    for handler in root.handlers:
        if not any(isinstance(f, RedactionFilter) for f in handler.filters):
            handler.addFilter(RedactionFilter())
    if already_configured:
        return
    handler = logging.StreamHandler()
    handler.addFilter(RedactionFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.setLevel(level)
    root.addHandler(handler)
