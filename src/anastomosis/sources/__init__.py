"""Source adapters: each module here turns one vendor's export into
canonical :class:`~anastomosis.core.model.PatientRecord` objects.

Adapters follow the brain-like modularity rule: isolated, individually
versioned, registered through :mod:`anastomosis.sources.base`, and loaded
defensively — a broken adapter reports a diagnosis instead of taking the
toolkit down.
"""

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
