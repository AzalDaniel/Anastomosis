"""The vessel mark, sampled onto a character grid. GENERATED — do not edit.

One digit per cell, 0 (nothing) to 4 (solid): the share of that cell the
mark covers, quantised to the density ramp :mod:`anastomosis.core.vesselmark`
draws with. Sampled from the same geometry as ``assets/icon/icon.svg`` by
``tools/make_vessel.py``, so the terminal greeting and the taskbar icon
cannot drift apart. Regenerate with ``python tools/make_vessel.py``;
``tests/unit/test_vesselmark.py`` re-samples the geometry and fails on any
mismatch with this file.
"""

from __future__ import annotations

#: Density levels, row by row, top of the mark first.
DENSITY: tuple[str, ...] = (
    "000001233332332210000",
    "001333343433444443100",
    "123333434332433444421",
    "133333334342433333332",
    "233344333433333343342",
    "133333333444443333331",
    "012222210030001222100",
    "000011100030001111000",
    "000000000030000000000",
    "000000000030000000000",
)

#: The top of the ramp — the level a cell the mark fills completely carries.
LEVELS = 4
