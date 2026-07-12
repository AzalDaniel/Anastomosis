# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""Logging with PHI redaction (security backlog: log redaction, from M1).

The discipline is "never log patient names, DOBs, or identifiers" — but
discipline fails, so this module is the defense-in-depth behind it:

* :func:`redact` scrubs SSN/phone/email/date shapes from any string.
* :class:`RedactionFilter` applies :func:`redact` to every record that
  passes through a handler, including interpolated args and exception text.
* :func:`exc_tag` is what error paths log instead of ``str(e)`` — exception
  *messages* frequently embed the input that caused them (a patient name in
  a parse error), while the exception *type* is always safe.
* :func:`safe_log_id` is a run-scoped HMAC surrogate for a source-derived
  identifier: loggable for within-run correlation, unlinkable across runs and
  unconfirmable against the source export without this process's ephemeral key.

Adapters and the pipeline must log **counts, field names, and safe_log_id
surrogates** — never values, and never raw source identifiers. The filter
exists for the day someone forgets.
"""

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
    """A loggable, run-scoped surrogate for a source-derived identifier.

    HMAC-SHA256 under a per-process ephemeral key: log lines about the same
    record correlate within a run, but surrogates are unlinkable across runs
    and cannot be confirmed against the source export without this run's key.
    The 12-hex (48-bit) truncation trades collision headroom for log
    readability — fine for correlation (collisions become likely only past
    ~10^7 distinct ids in one process), and a collision costs correlation
    quality, never data: real identifiers live in the ledger and reports.
    """
    if value is None or value == "":
        return "id:unknown"
    digest = hmac.new(_LOG_ID_KEY, str(value).encode("utf-8"), "sha256").hexdigest()[:12]
    return f"id:{digest}"


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


def _has_redacting_handler(logger: logging.Logger) -> bool:
    """True when ``logger`` already carries a handler with a RedactionFilter."""
    return any(
        any(isinstance(f, RedactionFilter) for f in handler.filters) for handler in logger.handlers
    )


def configure_logging(level: int = logging.WARNING) -> None:
    """Set up root logging with redaction installed on the handler.

    Idempotent: the two application entry points (the CLI callback and the
    GUI launcher) both call this, and a single process can hit both — so if
    the root logger already carries a redacting handler we return without
    stacking a second one (which would double-log every line).
    """
    root = logging.getLogger()
    if _has_redacting_handler(root):
        return
    handler = logging.StreamHandler()
    handler.addFilter(RedactionFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.setLevel(level)
    root.addHandler(handler)
