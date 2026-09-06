"""Tests for the PHI log-redaction layer.

All identifier-shaped strings below are synthetic: never-issued SSN range
(987-65-43xx), 555-01xx phone numbers, example.com addresses.
"""

import logging
import re

import pytest

from anastomosis.core.logutil import (
    RedactionFilter,
    configure_logging,
    exc_tag,
    redact,
    safe_log_id,
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
    # Non-padded dash and ISO variants are shapes too — a hand-typed date in
    # an error message doesn't come zero-padded.
    assert redact("dob 1-5-1980") == "dob [REDACTED-DATE]"
    assert redact("dob 1980-1-5") == "dob [REDACTED-DATE]"
    # The dash runs cannot collide with the SSN (3-2-4) or phone (3-3-4)
    # shapes, which are matched first: an SSN still redacts as an SSN.
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


def test_safe_log_id_is_stable_within_process() -> None:
    # Two calls on the same value correlate within a run — that is the whole
    # point of the surrogate (log lines about one record line up).
    guid = "feedface-0000-4000-8000-000000000001"
    assert safe_log_id(guid) == safe_log_id(guid)


def test_safe_log_id_distinguishes_distinct_values() -> None:
    assert safe_log_id("feedface-0000-4000-8000-000000000001") != safe_log_id(
        "feedface-0000-4000-8000-000000000002"
    )


@pytest.mark.parametrize("value", [None, ""])
def test_safe_log_id_sentinel_maps_to_unknown(value: object) -> None:
    assert safe_log_id(value) == "id:unknown"


@pytest.mark.parametrize(
    "value",
    [
        "feedface-0000-4000-8000-000000000001",
        12345,
        "encounter|feedface-0000-4000-8000-000000000009",
    ],
)
def test_safe_log_id_format(value: object) -> None:
    assert re.fullmatch(r"id:[0-9a-f]{12}", safe_log_id(value))


def test_safe_log_id_does_not_echo_the_source_value() -> None:
    # The surrogate must never contain the raw identifier: it is a digest, not
    # an encoding — the source GUID is unrecoverable from the log line.
    guid = "feedface-0000-4000-8000-000000000abc"
    assert guid not in safe_log_id(guid)


def _redacting_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [h for h in logger.handlers if any(isinstance(f, RedactionFilter) for f in h.filters)]


def test_configure_logging_is_idempotent() -> None:
    """Two entry points in one process must not stack redacting handlers."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        # Start from an empty root so the count below sees only what
        # configure_logging itself installs (the sweep would otherwise pull
        # the test runner's own capture handlers into the count).
        root.handlers[:] = []
        configure_logging()
        configure_logging()
        assert len(_redacting_handlers(root)) == 1
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_configure_logging_brings_preexisting_handlers_into_the_chain() -> None:
    """A raw handler installed BEFORE configure_logging must end up
    redacting: a host that calls ``logging.basicConfig`` first seeds
    the root with a raw StreamHandler, and every root handler must
    carry the RedactionFilter after configure_logging returns."""
    import io

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        root.handlers[:] = []
        sink = io.StringIO()
        preexisting = logging.StreamHandler(sink)
        root.addHandler(preexisting)
        configure_logging()
        assert _redacting_handlers(root) == root.handlers  # every handler redacts
        # Synthetic date-shaped value: the PRE-EXISTING handler must redact it.
        logging.getLogger("test.chain").warning("dob is 01/02/1990")
        assert "[REDACTED-DATE]" in sink.getvalue()
        assert "01/02/1990" not in sink.getvalue()
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_configure_logging_sweeps_late_handlers_without_stacking() -> None:
    """The reverse ordering: a raw handler added AFTER the first call is swept
    into the chain by the second call, and no second safe handler stacks."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        root.handlers[:] = []
        configure_logging()
        late = logging.StreamHandler()
        root.addHandler(late)
        assert late not in _redacting_handlers(root)
        configure_logging()
        assert _redacting_handlers(root) == root.handlers  # the late one redacts now
        assert len(root.handlers) == 2  # ours + the late one; nothing stacked
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
