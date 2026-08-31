"""Generate the vessel mark — the product logo — as deterministic SVG.

The mark is a corrosion-cast style vascular fan: one trunk entering from
below, two surgically cut major vessels (the anastomosis), and a canopy of
fine branching capillaries. It is GENERATED, not drawn: a seeded recursive
branching process writes ``assets/icon/icon.svg`` (full detail) and
``assets/icon/icon-small.svg`` (depth-limited, thicker — legible at 16-48 px).
Same seed in, same bytes out, so the checked-in SVGs are reproducible and a
parameter change is reviewable as a diff.

The same geometry has one more rendition to serve: a character grid, for the
terminal the guided session opens in. It is sampled from the full-detail SVG
this file already emits — the mark's coverage of each character cell, quantised
to a short density ramp — and written as
``src/anastomosis/core/vesselmark_data.py``. Sampling here rather than at
startup is the icon pipeline's own rule (the renditions are committed, because
a rendition only changes when the mark does), and it keeps the terminal
greeting free of geometry that could drift away from the logo:
``tests/unit/test_vesselmark.py`` re-samples and fails on any difference.

Run, then regenerate the raster renditions::

    python tools/make_vessel.py && python tools/make_icons.py
"""

from __future__ import annotations

import itertools
import math
import random
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_FULL = _ROOT / "assets" / "icon" / "icon.svg"
_SMALL = _ROOT / "assets" / "icon" / "icon-small.svg"
_GLYPH = _ROOT / "assets" / "icon" / "icon-glyph.svg"
_MATRIX = _ROOT / "src" / "anastomosis" / "core" / "vesselmark_data.py"

SIZE = 1024.0
SEED = 20260826

# Palette sampled from the reference mark: oxblood trunk, warmer fine vessels,
# porcelain ground.
GROUND = "#f0eade"
TRUNK = "#701a14"
MID = "#82261d"
FINE = "#96382c"
CAP_RIM = "#5d130f"

# Canopy ellipse the growth is confined to (fractions of the canvas).
CANOPY_CX, CANOPY_CY = 0.500, 0.470
CANOPY_RX, CANOPY_RY = 0.460, 0.330


def _canopy_radius(x: float, y: float) -> float:
    dx = (x / SIZE - CANOPY_CX) / CANOPY_RX
    dy = (y / SIZE - CANOPY_CY) / CANOPY_RY
    return math.hypot(dx, dy)


def _inside_canopy(x: float, y: float, slack: float = 1.0) -> bool:
    dx = (x / SIZE - CANOPY_CX) / (CANOPY_RX * slack)
    dy = (y / SIZE - CANOPY_CY) / (CANOPY_RY * slack)
    return dx * dx + dy * dy <= 1.0


class _Vessel:
    """One growth run: accumulates SVG path elements, deepest strokes first."""

    def __init__(self, max_depth: int, min_width: float, mono: bool = False) -> None:
        # Determinism, not cryptography: the seed IS the design.
        self.rng = random.Random(SEED)  # noqa: S311
        self.max_depth = max_depth
        self.min_width = min_width
        self.mono = mono
        self.layers: dict[int, list[str]] = {}

    def _emit(self, depth: int, d: str, width: float, color: str) -> None:
        self.layers.setdefault(depth, []).append(
            f'<path d="{d}" stroke="{color}" stroke-width="{width:.1f}" />'
        )

    def _color(self, depth: int) -> str:
        # The glyph tier is one flat dark colour: at 16-32 px anti-aliasing
        # washes a light stroke into the ground, so tonal depth costs contrast.
        if self.mono or depth <= 1:
            return TRUNK
        if depth <= 3:
            return MID
        return FINE

    def branch(
        self,
        x: float,
        y: float,
        angle: float,
        length: float,
        width: float,
        depth: int,
    ) -> None:
        """Grow one segment and recurse into its children.

        The segment is a quadratic curve with a small random bend; children
        fan out with widening spread as they get finer, and growth stops at
        the canopy ellipse, below the minimum width, or at max depth.
        """
        rng = self.rng
        if depth > self.max_depth or width < self.min_width:
            return
        bend = rng.uniform(-0.45, 0.45)
        mx = x + math.cos(angle + bend) * length * 0.55
        my = y + math.sin(angle + bend) * length * 0.55
        ex = x + math.cos(angle) * length
        ey = y + math.sin(angle) * length
        # Density fades toward the canopy rim instead of clipping at it —
        # a hard boundary reads as a shaved hedge, a fade reads as capillaries
        # running out of pressure.
        r = _canopy_radius(ex, ey)
        if r > 1.0:
            length *= 0.65
            ex = x + math.cos(angle) * length
            ey = y + math.sin(angle) * length
            if _canopy_radius(ex, ey) > 1.06:
                return
        d = f"M{x:.1f} {y:.1f} Q{mx:.1f} {my:.1f} {ex:.1f} {ey:.1f}"
        self._emit(depth, d, width, self._color(depth))

        # Perpendicular thorn stubs along fine vessels — the corrosion-cast
        # texture of the reference mark.
        if depth >= 3 and width < 9:
            for _ in range(rng.randint(1, 2)):
                t = rng.uniform(0.3, 0.9)
                sx = x + (ex - x) * t
                sy = y + (ey - y) * t
                side = rng.choice((-1.0, 1.0))
                sa = angle + side * math.pi / 2 + rng.uniform(-0.3, 0.3)
                sl = rng.uniform(8, 22) * (width / 6)
                tx = sx + math.cos(sa) * sl
                ty = sy + math.sin(sa) * sl
                if _inside_canopy(tx, ty, slack=1.1):
                    self._emit(
                        depth + 1,
                        f"M{sx:.1f} {sy:.1f} L{tx:.1f} {ty:.1f}",
                        max(width * 0.30, self.min_width * 0.8),
                        FINE,
                    )

        children = 3 if depth <= 4 and rng.random() < 0.5 else 2
        if r > 0.93 and rng.random() < (r - 0.93) * 5.0:
            children -= 1
        if children <= 0:
            return
        spread = rng.uniform(0.42, 0.68) + depth * 0.04
        for i in range(children):
            offset = (i - (children - 1) / 2) * spread + rng.uniform(-0.12, 0.12)
            self.branch(
                ex,
                ey,
                angle + offset,
                length * rng.uniform(0.72, 0.85),
                width * rng.uniform(0.60, 0.72),
                depth + 1,
            )

    def svg_body(self) -> list[str]:
        parts: list[str] = []
        for depth in sorted(self.layers, reverse=True):
            parts.extend(self.layers[depth])
        return parts


def _stump(
    x1: float, y1: float, qx: float, qy: float, x2: float, y2: float, width: float
) -> list[str]:
    """A cut major vessel: a thick curve ending in a blunt, rimmed cap."""
    d = f"M{x1:.1f} {y1:.1f} Q{qx:.1f} {qy:.1f} {x2:.1f} {y2:.1f}"
    angle = math.atan2(y2 - qy, x2 - qx)
    rim_rx = width * 0.52
    return [
        f'<path d="{d}" stroke="{TRUNK}" stroke-width="{width:.1f}" />',
        # Sheen: a lighter core line pulled slightly off-axis.
        f'<path d="{d}" stroke="#8d3126" stroke-width="{width * 0.34:.1f}" opacity="0.55" '
        f'transform="translate({-width * 0.10:.1f} {-width * 0.12:.1f})" />',
        # The lumen: a rimmed elliptical cap square to the vessel end.
        f'<ellipse cx="{x2:.1f}" cy="{y2:.1f}" rx="{rim_rx:.1f}" ry="{rim_rx * 0.72:.1f}" '
        f'transform="rotate({math.degrees(angle) + 90:.1f} {x2:.1f} {y2:.1f})" '
        f'fill="{CAP_RIM}" stroke="none" />',
    ]


def build(max_depth: int, min_width: float, with_ground: bool, bold: float = 1.0) -> str:
    """One rendition. ``bold`` scales every stroke — the 16-32 px glyph tier
    needs strokes heavy enough to survive rasterisation, not more detail."""
    v = _Vessel(max_depth, min_width, mono=bold > 1.5)
    hub_x, hub_y = 0.500 * SIZE, 0.615 * SIZE

    # Canopy: a fan of primary branches out of the hub. Angles in degrees,
    # measured screen-wise (negative y is up).
    primaries = (
        (-163, 0.27, 22),
        (-138, 0.32, 27),
        (-113, 0.35, 30),
        (-90, 0.36, 30),
        (-67, 0.35, 30),
        (-43, 0.32, 27),
        (-19, 0.27, 22),
        (-177, 0.20, 15),
        (-4, 0.20, 15),
    )
    for deg, frac, width in primaries:
        v.branch(hub_x, hub_y, math.radians(deg), frac * SIZE * 0.50, width * bold, 1)

    body = v.svg_body()

    # Trunk: below everything else in growth order but drawn on top, entering
    # from the base to the hub.
    trunk = [
        f'<path d="M{0.507 * SIZE:.1f} {0.965 * SIZE:.1f} '
        f'Q{0.494 * SIZE:.1f} {0.80 * SIZE:.1f} {hub_x:.1f} {hub_y:.1f}" '
        f'stroke="{TRUNK}" stroke-width="{30.0 * bold:.1f}" />',
        f'<ellipse cx="{0.507 * SIZE:.1f}" cy="{0.965 * SIZE:.1f}" rx="15.6" ry="11.2" '
        f'fill="{CAP_RIM}" stroke="none" />',
    ]
    # The two cut vessels of the anastomosis: up-left and to the right.
    stumps = _stump(
        hub_x, hub_y, 0.428 * SIZE, 0.505 * SIZE, 0.393 * SIZE, 0.338 * SIZE, 30.0 * bold
    ) + _stump(hub_x, hub_y, 0.645 * SIZE, 0.595 * SIZE, 0.735 * SIZE, 0.630 * SIZE, 25.0 * bold)

    ground = ""
    if with_ground:
        ground = f'<rect width="{SIZE:.0f}" height="{SIZE:.0f}" fill="{GROUND}" />'
    joined = "\n".join(body + trunk + stumps)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE:.0f} {SIZE:.0f}">\n'
        f"{ground}\n"
        f'<g fill="none" stroke-linecap="round" stroke-linejoin="round">\n'
        f"{joined}\n"
        f"</g>\n</svg>\n"
    )


# --- the character-grid rendition -------------------------------------------
#
# A terminal cell is roughly twice as tall as it is wide, so 21 x 11 cells over
# the square canvas keep the mark's proportions (each cell covers 48.8 x 93.1
# units of the 1024 canvas). Four density levels, because the terminal surface
# has exactly that much to spend on one: three text weights and one step in
# glyph size. A finer ramp would encode a gradient no terminal can show.

MATRIX_COLS = 21
MATRIX_ROWS = 11
MATRIX_LEVELS = 4
#: Coverage is compressed before it is quantised. The capillary canopy covers
#: about a third of a cell where the trunk covers all of one, and a linear ramp
#: put the entire canopy on the bottom step — a flat silhouette with a bright
#: stem, instead of a mark that thins toward the rim the way the logo does.
MATRIX_GAMMA = 0.6
#: Sub-cells per character cell, per axis. The coverage measure is their count.
_SUPERSAMPLE = 12

_PATH_RE = re.compile(r'<path d="([^"]+)" stroke="[^"]*" stroke-width="([0-9.]+)"')
_ELLIPSE_RE = re.compile(r'<ellipse cx="([0-9.]+)" cy="([0-9.]+)" rx="([0-9.]+)" ry="([0-9.]+)"')
_NUMBER_RE = re.compile(r"-?\d+\.?\d*")


def _flatten(d: str) -> list[tuple[float, float]]:
    """One path's ``d`` attribute as a polyline, quadratics subdivided."""
    numbers = [float(value) for value in _NUMBER_RE.findall(d)]
    if "Q" not in d:
        x1, y1, x2, y2 = numbers
        return [(x1, y1), (x2, y2)]
    x1, y1, qx, qy, x2, y2 = numbers
    steps = 20
    points = []
    for index in range(steps + 1):
        t = index / steps
        u = 1.0 - t
        points.append(
            (u * u * x1 + 2 * u * t * qx + t * t * x2, u * u * y1 + 2 * u * t * qy + t * t * y2)
        )
    return points


def _stamp(ink: bytearray, width: int, height: int, x: float, y: float, radius: float) -> None:
    """Ink every sub-cell whose centre falls under a disc of ``radius`` at (x, y).

    A disc per sample point is how a round-capped stroke covers area; the
    alternative — one sample per cell — measures the centre line and loses the
    stroke width the mark's whole tonal range is built out of.
    """
    cell_w, cell_h = SIZE / width, SIZE / height
    first_row = max(0, int((y - radius) / cell_h))
    last_row = min(height - 1, int((y + radius) / cell_h))
    first_col = max(0, int((x - radius) / cell_w))
    last_col = min(width - 1, int((x + radius) / cell_w))
    for row in range(first_row, last_row + 1):
        dy = (row + 0.5) * cell_h - y
        for col in range(first_col, last_col + 1):
            dx = (col + 0.5) * cell_w - x
            if dx * dx + dy * dy <= radius * radius:
                ink[row * width + col] = 1


def _ink(svg: str, width: int, height: int) -> bytearray:
    """Rasterise every stroke and cap in ``svg`` into a coverage bitmap."""
    ink = bytearray(width * height)
    stride = min(SIZE / width, SIZE / height) / 2.0
    for d, stroke in _PATH_RE.findall(svg):
        radius = float(stroke) / 2.0
        points = _flatten(d)
        for (ax, ay), (bx, by) in itertools.pairwise(points):
            steps = max(1, int(math.hypot(bx - ax, by - ay) / stride))
            for index in range(steps + 1):
                t = index / steps
                _stamp(ink, width, height, ax + (bx - ax) * t, ay + (by - ay) * t, radius)
    for cx, cy, rx, ry in _ELLIPSE_RE.findall(svg):
        _stamp(ink, width, height, float(cx), float(cy), max(float(rx), float(ry)))
    return ink


def _cell_level(ink: bytearray, width: int, row: int, col: int) -> int:
    """One cell's density level: what share of it the mark covers."""
    covered = sum(
        ink[y * width + x]
        for y in range(row * _SUPERSAMPLE, (row + 1) * _SUPERSAMPLE)
        for x in range(col * _SUPERSAMPLE, (col + 1) * _SUPERSAMPLE)
    )
    share = (covered / (_SUPERSAMPLE * _SUPERSAMPLE)) ** MATRIX_GAMMA
    return min(MATRIX_LEVELS, math.ceil(share * MATRIX_LEVELS))


def sample_matrix() -> tuple[str, ...]:
    """The mark as rows of density digits, one digit per character cell.

    Sampled from the full-detail rendition — the one the icon pipeline treats
    as the master — so the terminal mark is the same object as the icon, read
    at a coarser resolution. Rows the mark never reaches are trimmed, because
    a greeting should not spend a line of somebody's terminal on blank canvas.
    """
    svg = build(max_depth=10, min_width=1.0, with_ground=False)
    width, height = MATRIX_COLS * _SUPERSAMPLE, MATRIX_ROWS * _SUPERSAMPLE
    ink = _ink(svg, width, height)
    rows = [
        "".join(str(_cell_level(ink, width, row, col)) for col in range(MATRIX_COLS))
        for row in range(MATRIX_ROWS)
    ]
    while rows and set(rows[0]) == {"0"}:
        rows.pop(0)
    while rows and set(rows[-1]) == {"0"}:
        rows.pop()
    return tuple(rows)


_MATRIX_TEMPLATE = '''"""The vessel mark, sampled onto a character grid. GENERATED — do not edit.

One digit per character cell, 0 (nothing) to 4 (solid): the share of that cell
the mark covers, quantised to the density ramp
:mod:`anastomosis.core.vesselmark` draws with. The grid comes from the same
geometry as ``assets/icon/icon.svg`` — see ``tools/make_vessel.py``, which
writes this file — so the greeting a terminal shows and the icon on the
taskbar cannot drift apart. Regenerate with::

    python tools/make_vessel.py

``tests/unit/test_vesselmark.py`` re-samples the geometry and fails if this
file no longer matches it.
"""

from __future__ import annotations

#: Density levels, row by row, top of the mark first.
DENSITY: tuple[str, ...] = (
{rows}
)

#: The top of the ramp — the level a cell the mark fills completely carries.
LEVELS = {levels}
'''


def matrix_module_text() -> str:
    """The text of the generated ``core/vesselmark_data.py``."""
    rows = "\n".join(f'    "{row}",' for row in sample_matrix())
    return _MATRIX_TEMPLATE.format(rows=rows, levels=MATRIX_LEVELS)


def main() -> None:
    _FULL.write_text(build(max_depth=10, min_width=1.0, with_ground=True), encoding="utf-8")
    print(f"wrote {_FULL.relative_to(_ROOT)}")
    _SMALL.write_text(
        build(max_depth=5, min_width=6.0, with_ground=True, bold=1.25), encoding="utf-8"
    )
    print(f"wrote {_SMALL.relative_to(_ROOT)}")
    _GLYPH.write_text(
        build(max_depth=3, min_width=14.0, with_ground=True, bold=2.1), encoding="utf-8"
    )
    print(f"wrote {_GLYPH.relative_to(_ROOT)}")
    _MATRIX.write_text(matrix_module_text(), encoding="utf-8")
    print(f"wrote {_MATRIX.relative_to(_ROOT)}")


if __name__ == "__main__":
    main()
