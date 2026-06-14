"""Tests for the PHI log-redaction layer.

All identifier-shaped strings below are synthetic: never-issued SSN range
(987-65-43xx), 555-01xx phone numbers, example.com addresses.
"""

import logging

from anastomosis.core.logutil import RedactionFilter, exc_tag, redact


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
