"""Source adapters: each module here turns one vendor's export into
canonical :class:`~anastomosis.core.model.PatientRecord` objects, registered
through :mod:`anastomosis.sources.base`. See :mod:`anastomosis.sources.learned`
for how a taught (non-built-in) adapter is discovered defensively."""

from .base import (
    SelectionRule,
    SourceAdapter,
    SourceDataError,
    available_sources,
    detect_source,
    get_source,
    register,
    selection_rules,
    with_selection,
)

__all__ = [
    "SelectionRule",
    "SourceAdapter",
    "SourceDataError",
    "available_sources",
    "detect_source",
    "get_source",
    "register",
    "selection_rules",
    "with_selection",
]
