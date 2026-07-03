# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""Tests for the PHI log-redaction layer.

All identifier-shaped strings below are synthetic: never-issued SSN range
(987-65-43xx), 555-01xx phone numbers, example.com addresses.
"""

import logging

from anastomosis.core.logutil import (
    RedactionFilter,
    configure_logging,
    exc_tag,
    redact,
)


def test_redact_ssn_shape() -> None:
    assert redact("ssn 987-65-4320 on file") == "ssn [REDACTED-SSN] on file"


def test_redact_phone_shapes() -> None:
    assert "[REDACTED-PHONE]" in redact("call (206) 555-0123")
    assert "[REDACTED-PHONE]" in redact("call 206-555-0123")


def test_redact_email() -> None:
    assert redact("contact patient@example.com now") == "contact [REDACTED-EMAIL] now"


def test_redact_date_shapes() -> None:
    # The pattern is shape-based: any slash/ISO date in a message is treated
    # as input-derived and scrubbed, no surrounding keyword needed.
    assert redact("recorded 3/14/1990") == "recorded [REDACTED-DATE]"
    assert redact("dos 2019-03-14") == "dos [REDACTED-DATE]"


def test_redact_mm_dd_yyyy_shape() -> None:
    # The MM-DD-YYYY shape (the form rendered chart filenames embed) is
    # scrubbed too — defense-in-depth behind the discipline of never logging
    # such a filename in the first place.
    assert redact("dos 01-02-2020 recorded") == "dos [REDACTED-DATE] recorded"
    # The 2-2-4 run cannot collide with the SSN (3-2-4) or phone (3-3-4)
    # shapes: an SSN still redacts as an SSN, not as a date.
    assert redact("ssn 987-65-4320") == "ssn [REDACTED-SSN]"


def test_redact_leaves_clinical_counts_alone() -> None:
    msg = "mapped 42 encounters across 17 tables (3 skipped)"
    assert redact(msg) == msg


def _filtered(record: logging.LogRecord) -> str:
    RedactionFilter().filter(record)
    return record.getMessage()


def test_filter_scrubs_interpolated_args() -> None:
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="failed row for %s",
        args=("987-65-4320",),
        exc_info=None,
    )
    assert _filtered(record) == "failed row for [REDACTED-SSN]"


def test_filter_replaces_exception_text_with_type() -> None:
    try:
        raise ValueError("could not parse '3/14/1990' for patient")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="row rejected",
            args=(),
            exc_info=sys.exc_info(),
        )
    message = _filtered(record)
    assert "ValueError" in message
    assert "3/14/1990" not in message
    assert record.exc_info is None  # formatter can never render the raw traceback


def test_exc_tag_carries_no_message() -> None:
    tag = exc_tag(ValueError("patient Jane Doe rejected"))
    assert tag == "ValueError"


def _redacting_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [h for h in logger.handlers if any(isinstance(f, RedactionFilter) for f in h.filters)]


def test_configure_logging_is_idempotent() -> None:
    """Two entry points in one process must not stack redacting handlers."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        # Start from a clean slate: drop any redacting handler a prior test
        # (or an entry-point call) installed on the shared root logger.
        root.handlers[:] = [h for h in root.handlers if h not in _redacting_handlers(root)]
        configure_logging()
        configure_logging()
        assert len(_redacting_handlers(root)) == 1
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
