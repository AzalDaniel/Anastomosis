"""Insurance coverage (the section with the infamous PlanType fallback join)."""

from __future__ import annotations

from datetime import date

from .base import AnastBase


class Coverage(AnastBase):
    patient_id: str
    payer: str | None = None
    plan_name: str | None = None
    # HMO/PPO/EPO/... — sourced via a multi-step join in some exports; never
    # confuse with coverage_type, which is the benefit domain ("Medical").
    plan_type: str | None = None
    coverage_type: str | None = None
    member_id: str | None = None
    group_number: str | None = None
    order_of_benefits: int | None = None  # 0=primary, 1=secondary, ...
    priority_label: str | None = None  # "PRIMARY PAYER" as printed
    employer: str | None = None
    relationship_to_insured: str | None = None
    payment_type: str | None = None
    copay: str | None = None  # display-formatted; "-" semantics live in render
    start: date | None = None
    end: date | None = None
    active: bool = True
    status_label: str | None = None
