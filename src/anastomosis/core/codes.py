"""Terminology data: the LOINC vital-sign map, pain answer codes, BMI math.

Codes live here as plain data so every adapter, template pack, and the FHIR
exporter agree on one spelling of each concept. Units are the UCUM codes US
ambulatory exports chart in by convention — adapters override them when a
source declares its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

__all__ = ["PAIN_SEVERITY_LA", "VITALS", "VitalCode", "bmi_imperial", "bmi_metric"]


@dataclass(frozen=True, slots=True)
class VitalCode:
    loinc: str
    display: str
    unit: str  # default UCUM unit; adapters override from source metadata


VITALS: Mapping[str, VitalCode] = MappingProxyType(
    {
        "height": VitalCode("8302-2", "Body height", "[in_i]"),
        "weight": VitalCode("29463-7", "Body weight", "[lb_av]"),
        "bmi": VitalCode("39156-5", "Body mass index (BMI) [Ratio]", "kg/m2"),
        "systolic_bp": VitalCode("8480-6", "Systolic blood pressure", "mm[Hg]"),
        "diastolic_bp": VitalCode("8462-4", "Diastolic blood pressure", "mm[Hg]"),
        "heart_rate": VitalCode("8867-4", "Heart rate", "/min"),
        "respiratory_rate": VitalCode("9279-1", "Respiratory rate", "/min"),
        "temperature": VitalCode("8310-5", "Body temperature", "[degF]"),
        "oxygen_saturation": VitalCode(
            "59408-5", "Oxygen saturation in Arterial blood by Pulse oximetry", "%"
        ),
        "head_circumference": VitalCode("9843-4", "Head Occipital-frontal circumference", "[in_i]"),
        "pain_severity": VitalCode(
            "72514-3", "Pain severity - 0-10 verbal numeric rating [Score]", "{score}"
        ),
    }
)

# LOINC answer codes for the 0-10 pain scale (answer list for 72514-3).
PAIN_SEVERITY_LA: Mapping[int, str] = MappingProxyType(
    {
        0: "LA6111-4",
        1: "LA6112-2",
        2: "LA6113-0",
        3: "LA6114-8",
        4: "LA6115-5",
        5: "LA10137-0",
        6: "LA10138-8",
        7: "LA10139-6",
        8: "LA10140-4",
        9: "LA10141-2",
        10: "LA13942-0",
    }
)


def bmi_metric(weight_kg: float | None, height_cm: float | None) -> float | None:
    """BMI (kg/m², 1 decimal) from metric vitals; ``None`` when not computable."""
    if not weight_kg or not height_cm or weight_kg <= 0 or height_cm <= 0:
        return None
    return round(weight_kg / (height_cm / 100.0) ** 2, 1)


def bmi_imperial(weight_lb: float | None, height_in: float | None) -> float | None:
    """BMI (kg/m², 1 decimal) from lb/inches — the PF/Tebra charting units.

    Sources frequently chart height and weight but omit BMI; the adapter's
    BMI auto-calc fills the gap with this (the standard CDC 703 factor).
    """
    if not weight_lb or not height_in or weight_lb <= 0 or height_in <= 0:
        return None
    return round(703.0 * weight_lb / height_in**2, 1)
