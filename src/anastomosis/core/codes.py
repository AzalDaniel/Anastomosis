"""Terminology data: the LOINC vital-sign map, pain answer codes, BMI math.

Codes live here as plain data so every adapter, template pack, and the FHIR
exporter agree on one spelling of each concept. Units are the UCUM codes US
ambulatory exports chart in by convention — adapters override them when a
source declares its own.

The PRIMARY LOINC for each vital is the one the battle-tested predecessor
charted against (generate_pdfs.py:540-553, the ``LOINC`` table that ran on
12,906 real documents). Modern C-CDA / Synthea editions legitimately chart
some vitals under newer LOINC siblings, so those ride along as ``aliases``
that resolve to the same vital kind (dual-map, old code first).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

__all__ = [
    "PAIN_LA_TO_LEVEL",
    "PAIN_SEVERITY_LA",
    "VITALS",
    "VitalCode",
    "bmi_imperial",
    "bmi_metric",
    "pain_display",
]


@dataclass(frozen=True, slots=True)
class VitalCode:
    loinc: str  # PRIMARY code (predecessor generate_pdfs.py:540-553)
    display: str
    unit: str  # default UCUM unit; adapters override from source metadata
    aliases: tuple[str, ...] = field(default=())  # newer C-CDA/Synthea editions


VITALS: Mapping[str, VitalCode] = MappingProxyType(
    {
        "height": VitalCode("8302-2", "Body height", "[in_i]"),
        # weight: predecessor 3141-9 (gpdfs:542) primary; 29463-7 is the modern
        # "Body weight" sibling C-CDA/Synthea use — both are body weight.
        "weight": VitalCode("3141-9", "Body weight", "[lb_av]", aliases=("29463-7",)),
        "bmi": VitalCode("39156-5", "Body mass index (BMI) [Ratio]", "kg/m2"),
        # BMI percentile: predecessor 59576-9 (gpdfs:544) — was absent from ours.
        "bmi_percentile": VitalCode("59576-9", "Body mass index (BMI) [Percentile]", "%"),
        "systolic_bp": VitalCode("8480-6", "Systolic blood pressure", "mm[Hg]"),
        "diastolic_bp": VitalCode("8462-4", "Diastolic blood pressure", "mm[Hg]"),
        "heart_rate": VitalCode("8867-4", "Heart rate", "/min"),
        "respiratory_rate": VitalCode("9279-1", "Respiratory rate", "/min"),
        "temperature": VitalCode("8310-5", "Body temperature", "[degF]"),
        # O2 sat: predecessor 2708-6 (gpdfs:550) primary; 59408-5 is the modern
        # "by Pulse oximetry" sibling — both are arterial SpO2.
        "oxygen_saturation": VitalCode(
            "2708-6", "Oxygen saturation in Arterial blood", "%", aliases=("59408-5",)
        ),
        "pain_severity": VitalCode(
            "72514-3", "Pain severity - 0-10 verbal numeric rating [Score]", "{score}"
        ),
        # head circumference: predecessor 8287-5 (gpdfs:552) primary; 9843-4 is
        # the modern "Head Occipital-frontal circumference" sibling.
        "head_circumference": VitalCode(
            "8287-5", "Head Occipital-frontal circumference", "[in_i]", aliases=("9843-4",)
        ),
    }
)

# LOINC answer codes for the 0-10 pain scale (answer list for 72514-3).
# 0-4 = LA6111-4…LA6115-5 and 5-10 = LA6116-3…LA6121-3 are the predecessor's
# _PAIN_LA_MAP (generate_pdfs.py:515-518) — the LA61xx run PF actually emits.
# 5-10 also carry the modern LA10137-0… answer list as aliases (see
# PAIN_LA_TO_LEVEL) so newer exports still translate.
PAIN_SEVERITY_LA: Mapping[int, str] = MappingProxyType(
    {
        0: "LA6111-4",
        1: "LA6112-2",
        2: "LA6113-0",
        3: "LA6114-8",
        4: "LA6115-5",
        5: "LA6116-3",
        6: "LA6117-1",
        7: "LA6118-9",
        8: "LA6119-7",
        9: "LA6120-5",
        10: "LA6121-3",
    }
)

# Inverse map (answer code → numeric level), the predecessor's _PAIN_LA_MAP
# direction (generate_pdfs.py:515-518). The LA61xx run is primary (old-first);
# the LA10137-0… run is kept as accepted aliases for 5-10 AFTER the old codes.
PAIN_LA_TO_LEVEL: Mapping[str, int] = MappingProxyType(
    {
        # predecessor _PAIN_LA_MAP — the codes PF charts pain against
        "LA6111-4": 0,
        "LA6112-2": 1,
        "LA6113-0": 2,
        "LA6114-8": 3,
        "LA6115-5": 4,
        "LA6116-3": 5,
        "LA6117-1": 6,
        "LA6118-9": 7,
        "LA6119-7": 8,
        "LA6120-5": 9,
        "LA6121-3": 10,
        # fallback aliases: the modern 5-10 answer list, accepted AFTER the old map
        "LA10137-0": 5,
        "LA10138-8": 6,
        "LA10139-6": 7,
        "LA10140-4": 8,
        "LA10141-2": 9,
        "LA13942-0": 10,
    }
)


def pain_display(value: str | None) -> str | None:
    """Convert a pain answer code or numeric string to its 0-10 display value.

    Ports the predecessor's ``_pain_conv`` (generate_pdfs.py:520-538): strip an
    optional ``LOINC:`` prefix, translate a known LA answer code to its number,
    accept a raw 0-10 numeric, else fall back to the raw value (lossless).
    """
    if not value:
        return None
    s = str(value).strip()
    if s.startswith("LOINC:"):  # gpdfs:526 — strip possible "LOINC:" prefix
        s = s[6:]
    if s in PAIN_LA_TO_LEVEL:  # gpdfs:529 — mapped answer code
        return str(PAIN_LA_TO_LEVEL[s])
    try:  # gpdfs:532 — raw numeric 0-10
        n = int(float(s))
        if 0 <= n <= 10:
            return str(n)
    except (ValueError, TypeError):
        pass
    return s  # gpdfs:538 — fallback to raw


def bmi_metric(weight_kg: float | None, height_cm: float | None) -> float | None:
    """BMI (kg/m², 2 decimals) from metric vitals; ``None`` when not computable.

    Two decimals matches the predecessor's BMI rendering (generate_pdfs.py:543
    ``f"{float(v):.2f}"`` and the auto-calc at :592).
    """
    if not weight_kg or not height_cm or weight_kg <= 0 or height_cm <= 0:
        return None
    return round(weight_kg / (height_cm / 100.0) ** 2, 2)


def bmi_imperial(weight_lb: float | None, height_in: float | None) -> float | None:
    """BMI (kg/m², 2 decimals) from lb/inches — the PF/Tebra charting units.

    Sources frequently chart height and weight but omit BMI; the adapter's
    BMI auto-calc fills the gap with this (the standard CDC 703 factor).
    Two decimals matches the predecessor (generate_pdfs.py:543,592).
    """
    if not weight_lb or not height_in or weight_lb <= 0 or height_in <= 0:
        return None
    return round(703.0 * weight_lb / height_in**2, 2)
