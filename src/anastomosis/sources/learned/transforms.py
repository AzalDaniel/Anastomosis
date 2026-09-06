"""The closed set of value transforms a learned mapping may name.

A mapping is data, and data must never carry code: no lambda, no ``eval``.
It names a transform from this fixed verb table instead (``verb``,
``verb:arg``, ``verb:arg1:arg2``), each single-input. Arity is validated
at LOAD time (:func:`parse_transform`), so a typo'd verb is a loud
:class:`MappingError`, never a per-row surprise. Every transform returns
``None`` for an empty/sentinel input, matching
:mod:`anastomosis.core.textutil`/``timeutil`` sentinel handling. PHI:
errors name the verb only, never the value."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from anastomosis.core.textutil import clean_cell, clean_numeric, format_phone
from anastomosis.core.timeutil import parse_date, parse_dt

__all__ = ["TransformError", "apply_transform", "parse_transform"]


class TransformError(ValueError):
    """A transform spec is unknown or malformed (raised at mapping load time)."""


# A bound transform: the raw cell in, the canonical value out.
_Transform = Callable[[str | None], Any]


def _strptime_date(fmt: str) -> _Transform:
    def run(raw: str | None) -> Any:
        text = clean_cell(raw)
        if text is None:
            return None
        return datetime.strptime(text, fmt).date()  # noqa: DTZ007 — date-only field

    return run


def _strptime_dt(fmt: str) -> _Transform:
    def run(raw: str | None) -> Any:
        text = clean_cell(raw)
        if text is None:
            return None
        # Naive parse; attach UTC (the source-DB convention) like timeutil does.
        from datetime import UTC

        return datetime.strptime(text, fmt).replace(tzinfo=UTC)

    return run


def _split(delimiter: str, index_text: str) -> _Transform:
    try:
        index = int(index_text)
    except ValueError:
        raise TransformError(f"split index must be an integer, got {index_text!r}") from None

    def run(raw: str | None) -> Any:
        text = clean_cell(raw)
        if text is None:
            return None
        parts = text.split(delimiter)
        if -len(parts) <= index < len(parts):
            return clean_cell(parts[index])
        return None  # index past the end of this row's value — nothing there

    return run


def _const(value: str) -> _Transform:
    return lambda _raw: value


def _upper(raw: str | None) -> Any:
    text = clean_cell(raw)
    return text.upper() if text is not None else None


def _lower(raw: str | None) -> Any:
    text = clean_cell(raw)
    return text.lower() if text is not None else None


# No-argument verbs, resolved directly.
_NULLARY: dict[str, _Transform] = {
    # identity preserves the cell verbatim (only None passes through); use it
    # when an exact string must survive untouched.
    "identity": lambda raw: raw,
    # strip is the sensible default: trims and maps null-sentinels to None.
    "strip": clean_cell,
    "upper": _upper,
    "lower": _lower,
    "phone": format_phone,
    "numeric": clean_numeric,
    # No-arg date/datetime use the multi-format parsers (ISO + the C-CDA
    # spellings already handled); the ``:FMT`` forms below pin one format.
    "parse_date": parse_date,
    "parse_datetime": parse_dt,
}

# Verbs that take arguments: name -> (argument count, factory).
_PARAMETRIC: dict[str, tuple[int, Callable[..., _Transform]]] = {
    "parse_date": (1, _strptime_date),
    "parse_datetime": (1, _strptime_dt),
    "split": (2, _split),
    "const": (1, _const),
}


#: Verbs whose OUTPUT does not determine their INPUT (``const`` discards the
#: cell, ``split`` keeps one piece, ``upper``/``lower`` fold case away). A
#: column read through one of these must ALSO keep its raw value in
#: ``extensions``; the interpreter and the round-trip proof both consult this
#: set, so an added verb without preservation fails the proof.
LOSSY_TRANSFORMS = frozenset({"const", "split", "upper", "lower"})


def is_lossy(spec: str) -> bool:
    """Whether ``spec``'s verb cannot reproduce the cell it was given."""
    return spec.split(":", 1)[0] in LOSSY_TRANSFORMS


def parse_transform(spec: str) -> _Transform:
    """Resolve a transform spec string to a bound transform, or raise
    :class:`TransformError` for an unknown verb or wrong arity — at LOAD
    time, before any row is read. Arguments split on the verb's KNOWN
    arity, not blindly on every ``:``, so a single-argument value may
    itself contain colons (``parse_datetime:%Y-%m-%d %H:%M``)."""
    verb, sep, rest = spec.partition(":")
    if sep == "":  # a bare verb, no arguments
        if verb in _NULLARY:
            return _NULLARY[verb]
        if verb in _PARAMETRIC:
            raise TransformError(f"transform {verb!r} needs {_PARAMETRIC[verb][0]} argument(s)")
        raise TransformError(f"unknown transform {spec!r}")
    if verb in _PARAMETRIC:
        arity, factory = _PARAMETRIC[verb]
        if rest == "":
            raise TransformError(f"transform {verb!r} needs {arity} argument(s)")
        args = rest.split(":", arity - 1)
        if len(args) != arity:
            raise TransformError(f"transform {verb!r} takes {arity} argument(s), got {len(args)}")
        return factory(*args)
    if verb in _NULLARY:
        raise TransformError(f"transform {verb!r} takes no arguments")
    raise TransformError(f"unknown transform {spec!r}")


def apply_transform(spec: str, raw: str | None) -> Any:
    """Parse ``spec`` and apply it to ``raw`` (convenience for one-off use)."""
    return parse_transform(spec)(raw)
