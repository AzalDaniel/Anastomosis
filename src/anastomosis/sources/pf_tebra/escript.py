"""Prescription status resolution from transaction history.

``prescription-transactions.tsv`` records every status transition of a
script ("Order Sent" → "Verified" → "Dispensed"…). Charts print a single
resolved label, so transitions collapse by priority: terminal failure
states beat success states beat in-flight states, regardless of row order
(real exports do not guarantee chronological rows).
"""

from __future__ import annotations

from anastomosis.core.model import PrescriptionTransaction

__all__ = ["resolve_status", "script_prefix"]

# Higher wins. Project-defined ordering (the export documents no value set):
# a script that errored or was cancelled must never display as sent/filled.
_PRIORITY = {
    "error": 7,
    "failed": 7,
    "cancelled": 6,
    "canceled": 6,
    "dispensed": 5,
    "verified": 4,
    "sent": 3,
    "recorded": 2,
    "printed": 1,
}


def resolve_status(transactions: list[PrescriptionTransaction]) -> str | None:
    """Collapse a transaction history to one display label (uppercased)."""
    best: str | None = None
    best_rank = 0
    for tx in transactions:
        label = (tx.kind or "").strip()
        rank = _PRIORITY.get(label.lower(), 0)
        if rank > best_rank:
            best, best_rank = label.upper(), rank
    if best is None and transactions:
        # Unknown statuses still resolve (lossless: show what the source said).
        last = transactions[-1].kind
        return last.upper() if last else None
    return best


def script_prefix(destination_type_code: str | None) -> str:
    """Display prefix: electronically sent scripts are ESCRIPTs, the rest
    (printed, recorded-only) are plain SCRIPTs."""
    return "ESCRIPT" if (destination_type_code or "").upper() == "SEND" else "SCRIPT"
