"""Prescription status resolution from transaction history.

``prescription-transactions.tsv`` records every status transition of a
script ("Order Sent" → "Verified" → "Dispensed"…). Charts print a single
resolved label, so transitions collapse by priority.

The label map and priority table are ported verbatim from the battle-tested
predecessor (``generate_pdfs.py`` §5, lines 331-405) — the 20-description
``_ESCRIPT_LABEL_MAP`` and the granular ``_ESCRIPT_PRIORITY`` derived
empirically from 12,906 real documents. The governing rule that table encodes:
cancellations override Verified, dispensing overrides cancellations, and
refills / changes / clarifications DO NOT override Verified.
"""

from __future__ import annotations

from datetime import datetime

from anastomosis.core.model import PrescriptionTransaction
from anastomosis.core.timeutil import to_local

__all__ = ["resolve_display_date", "resolve_prefix", "resolve_status", "script_prefix"]

# Practices charting on Practice Fusion are US/Eastern; the predecessor hard-
# rolled UTC→Eastern (generate_pdfs.py:28,425). We converge on the IANA zone
# via core.timeutil.to_local (zoneinfo) — same wall-clock result, DST-correct.
_DISPLAY_TZ = "America/New_York"

# Transaction description (lowercased) → (prefix, status_label).
# Ported verbatim from generate_pdfs.py:331-351 (_ESCRIPT_LABEL_MAP).
# prefix is "ESCRIPT" for electronic, "SCRIPT" for printed/paper.
_ESCRIPT_LABEL_MAP: dict[str, tuple[str, str]] = {
    "order sent": ("ESCRIPT", "VERIFIED"),
    "prescription printed": ("SCRIPT", "PRINTED"),
    "cancellation request sent to pharmacy": ("ESCRIPT", "CANCELLATION REQUESTED"),
    "cancellation approved by pharmacy": ("ESCRIPT", "CANCELLED"),
    "cancellation denied by pharmacy": ("ESCRIPT", "CANCELLATION DENIED"),
    "prescription dispensed": ("ESCRIPT", "DISPENSED"),
    "prescription partially dispensed": ("ESCRIPT", "PARTIALLY DISPENSED"),
    "prescription not dispensed": ("ESCRIPT", "NOT DISPENSED"),
    "refill request received": ("ESCRIPT", "REFILL REQUEST RECEIVED"),
    "refill request approved": ("ESCRIPT", "REFILL REQUEST APPROVED"),
    "refill request denied": ("ESCRIPT", "REFILL REQUEST DENIED"),
    "refill request replaced": ("ESCRIPT", "REFILL REQUEST REPLACED"),
    "change request approved": ("ESCRIPT", "CHANGE REQUEST APPROVED"),
    "change request denied": ("ESCRIPT", "CHANGE REQUEST DENIED"),
    "therapeutic interchange request received": ("ESCRIPT", "INTERCHANGE REQUEST RECEIVED"),
    "script clarification": ("ESCRIPT", "CLARIFICATION"),
    "prescription recorded": ("SCRIPT", "RECORDED"),
    "out of stock": ("ESCRIPT", "OUT OF STOCK"),
    "prior authorization request received": ("ESCRIPT", "PRIOR AUTH REQUESTED"),
}

# Priority ranking when one prescription has multiple transactions. Highest
# wins. Ported verbatim from generate_pdfs.py:362-383 (_ESCRIPT_PRIORITY):
# dispensing > cancellation > Verified; refills/changes/clarifications (10) DO
# NOT override Verified (50).
_ESCRIPT_PRIORITY: dict[str, int] = {
    "DISPENSED": 100,
    "PARTIALLY DISPENSED": 95,
    "NOT DISPENSED": 92,
    "CANCELLED": 90,
    "CANCELLATION DENIED": 85,
    "CANCELLATION REQUESTED": 80,
    "VERIFIED": 50,  # baseline "order sent"
    "PRINTED": 48,
    "RECORDED": 45,
    # The following never override VERIFIED (lower priority):
    "CHANGE REQUEST APPROVED": 10,
    "CHANGE REQUEST DENIED": 10,
    "REFILL REQUEST APPROVED": 10,
    "REFILL REQUEST DENIED": 10,
    "REFILL REQUEST REPLACED": 10,
    "REFILL REQUEST RECEIVED": 10,
    "INTERCHANGE REQUEST RECEIVED": 10,
    "CLARIFICATION": 10,
    "OUT OF STOCK": 10,
    "PRIOR AUTH REQUESTED": 10,
}

# Fallback priority keyed on the clean Status word (our model's tx.kind), used
# only AFTER the predecessor's description map yields nothing — keeps the new
# adapter's coarser status words working without overriding the old map.
_FALLBACK_PRIORITY: dict[str, int] = {
    "error": 100,
    "failed": 100,
    "cancelled": 90,
    "canceled": 90,
    "dispensed": 100,
    "verified": 50,
    "sent": 40,
    "recorded": 45,
    "printed": 48,
}


def _resolve_via_label_map(
    transactions: list[PrescriptionTransaction],
) -> tuple[str, str] | None:
    """Predecessor resolve_script_label core (gpdfs:385-399): match each
    transaction's description against _ESCRIPT_LABEL_MAP, keep the highest
    priority. Returns (prefix, label) or None when nothing matched."""
    best: tuple[int, str, str] | None = None
    for tx in transactions:
        description = (tx.description or "").strip().lower()
        if description in _ESCRIPT_LABEL_MAP:
            prefix, label = _ESCRIPT_LABEL_MAP[description]
            priority = _ESCRIPT_PRIORITY.get(label, 10)
            if best is None or priority > best[0]:
                best = (priority, prefix, label)
    if best is not None:
        return best[1], best[2]
    return None


def resolve_status(transactions: list[PrescriptionTransaction]) -> str | None:
    """Collapse a transaction history to one display label (uppercased).

    The predecessor's _ESCRIPT_LABEL_MAP (keyed on TransactionDescription) is
    primary; our Status-word fallback runs only when no description matched
    (so VERIFIED still beats a refill that the old map ranks at 10).
    """
    resolved = _resolve_via_label_map(transactions)
    if resolved is not None:
        return resolved[1]
    # Fallback on the clean Status word (gpdfs had no such column; ours does).
    best: str | None = None
    best_rank = 0
    for tx in transactions:
        label = (tx.kind or "").strip()
        rank = _FALLBACK_PRIORITY.get(label.lower(), 0)
        if rank > best_rank:
            best, best_rank = label.upper(), rank
    if best is None and transactions:
        # Unknown statuses still resolve (lossless: show what the source said).
        last = transactions[-1].kind
        return last.upper() if last else None
    return best


def resolve_prefix(
    transactions: list[PrescriptionTransaction], destination_type_code: str | None
) -> str:
    """Display prefix, preferring the label map's prefix (gpdfs:394), then the
    predecessor's paper/print destination inference (gpdfs:402-405)."""
    resolved = _resolve_via_label_map(transactions)
    if resolved is not None:
        return resolved[0]
    return script_prefix(destination_type_code)


def script_prefix(destination_type_code: str | None) -> str:
    """Destination-based prefix fallback (predecessor gpdfs:402-405): paper /
    print destinations are plain SCRIPTs, everything else is an ESCRIPT."""
    dt = (destination_type_code or "").lower()
    if "paper" in dt or "print" in dt:
        return "SCRIPT"
    return "ESCRIPT"


def resolve_display_date(
    transactions: list[PrescriptionTransaction],
    prefix: str,
    fallback: datetime | None,
) -> datetime | None:
    """The date the escript line shows (predecessor resolve_script_display_date,
    gpdfs:408-429).

    For ESCRIPT: the earliest "Order sent" transaction datetime, converted to
    practice-local (Eastern) time. For SCRIPT or when no Order-sent txn exists:
    the prescription DoS (``fallback``), used as-is.
    """
    if prefix == "ESCRIPT":
        order_sent: datetime | None = None
        for tx in transactions:
            description = (tx.description or "").strip().lower()
            if description == "order sent" and tx.at is not None:
                if order_sent is None or tx.at < order_sent:
                    order_sent = tx.at
        if order_sent is not None:
            return to_local(order_sent, _DISPLAY_TZ)
    return fallback
