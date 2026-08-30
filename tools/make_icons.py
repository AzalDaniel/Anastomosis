"""Regenerate every branding rendition from the one SVG master.

``assets/icon/icon.svg`` is the single source of truth for the product
mark. This script derives every consumer-facing rendition from it:

* ``assets/icon/icon.ico`` — multi-resolution (16/24/32/48/64/128/256)
  Windows icon, consumed by Nuitka (``--windows-icon-from-ico``) and Inno
  Setup (``SetupIconFile``), which is what puts the mark on the exe, the
  taskbar, the Start menu, and Add/Remove Programs.
* ``assets/installer/wizard.bmp`` — the Inno Setup ``WizardImageFile``
  (the tall left banner of the install wizard).
* ``assets/installer/wizard-small.bmp`` — the Inno Setup
  ``WizardSmallImageFile`` (the top-right header mark).
* ``packaging/msix-assets/*.png`` — the three logo renditions an
  ``AppxManifest.xml`` must name (``Square150x150Logo``,
  ``Square44x44Logo``, ``StoreLogo``), consumed by
  ``packaging/build_msix.py`` when it stages the Microsoft Store package.
  They are COMMITTED rather than rendered during the packaging build,
  because this script's two renderers (cairosvg, Pillow) are dev-only
  tools and the Windows packaging job has neither — adding them there
  would buy an extra dependency on the release path for three small
  files that only change when the mark does.

Run after any edit to the SVG::

    python tools/make_icons.py

Requires ``cairosvg`` and ``Pillow`` (dev-only tools, not runtime
dependencies): ``pip install cairosvg pillow``.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

_ROOT = Path(__file__).resolve().parent.parent
_SVG = _ROOT / "assets" / "icon" / "icon.svg"
_SVG_SMALL = _ROOT / "assets" / "icon" / "icon-small.svg"
_SVG_GLYPH = _ROOT / "assets" / "icon" / "icon-glyph.svg"
_ICO = _ROOT / "assets" / "icon" / "icon.ico"
_WIZARD = _ROOT / "assets" / "installer" / "wizard.bmp"
_WIZARD_SMALL = _ROOT / "assets" / "installer" / "wizard-small.bmp"
_MSIX_ASSETS = _ROOT / "packaging" / "msix-assets"

# Explorer/taskbar/Start-menu coverage per Windows iconography guidance.
_ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

# Inno Setup 6 wizard image baseline sizes (Inno scales for DPI from these).
_WIZARD_SIZE = (164, 314)
_WIZARD_SMALL_SIZE = (55, 58)

# The MSIX logo set, keyed by the filename packaging/AppxManifest.xml.in names.
# Three is the documented minimum for a packaged desktop app: the medium tile
# (150), the app-list icon (44), and the 50 px Store logo. They get the SAME
# rounded-tile treatment as the .ico renditions on purpose — the Start menu
# shows the .ico for an installer install and these PNGs for a Store install,
# and one product should not have two marks depending on how it arrived.
_MSIX_LOGO_SIZES = {
    "Square150x150Logo.png": 150,
    "Square44x44Logo.png": 44,
    "StoreLogo.png": 50,
}

# The app's dark warm ground — matches the SVG tile and the GUI wallpaper.
_GROUND = (0x17, 0x13, 0x10)


def _master_for(size: int) -> Path:
    """The right master per rendition size — the vessel mark is generated in
    three detail tiers (tools/make_vessel.py) because the full canopy
    rasterises to mush below ~48 px while the bold glyph looks crude above it.
    """
    if size <= 32:
        return _SVG_GLYPH
    if size <= 64:
        return _SVG_SMALL
    return _SVG


def _render(size: int) -> PILImage:
    import cairosvg  # type: ignore[import-untyped]
    from PIL import Image

    png = cairosvg.svg2png(url=str(_master_for(size)), output_width=size, output_height=size)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def _rounded(image: PILImage, radius_frac: float = 0.22) -> PILImage:
    """The tile shape: the porcelain ground clipped to a rounded square, so
    the exe/taskbar icon reads as an app tile rather than a raw screenshot."""
    from PIL import Image, ImageDraw

    mask = Image.new("L", image.size, 0)
    draw = ImageDraw.Draw(mask)
    radius = int(min(image.size) * radius_frac)
    draw.rounded_rectangle((0, 0, image.size[0] - 1, image.size[1] - 1), radius, fill=255)
    out = image.copy()
    out.putalpha(mask)
    return out


def _flatten(image: PILImage) -> PILImage:
    """BMP has no alpha channel worth trusting — composite onto the ground."""
    from PIL import Image

    base = Image.new("RGB", image.size, _GROUND)
    base.paste(image, mask=image.split()[3])
    return base


def _banner(size: tuple[int, int], mark_px: int) -> PILImage:
    from PIL import Image

    mark = _rounded(_render(mark_px))
    canvas = Image.new("RGBA", size, (*_GROUND, 255))
    canvas.paste(mark, ((size[0] - mark.width) // 2, (size[1] - mark.height) // 2), mark)
    return _flatten(canvas)


def _write_msix_logos() -> None:
    """The three PNG renditions the Microsoft Store package names.

    Same masters, same rounded tile, same source of truth as every other
    rendition — only the sizes and the container differ.
    """
    _MSIX_ASSETS.mkdir(parents=True, exist_ok=True)
    for name, size in sorted(_MSIX_LOGO_SIZES.items()):
        out = _MSIX_ASSETS / name
        _rounded(_render(size)).save(out, format="PNG")
        print(f"wrote {out.relative_to(_ROOT)} ({size}x{size})")


def main() -> int:
    if not _SVG.is_file():
        print(f"missing SVG master: {_SVG}", file=sys.stderr)
        return 2

    renditions = [_rounded(_render(s)) for s in _ICO_SIZES]
    _ICO.parent.mkdir(parents=True, exist_ok=True)
    renditions[-1].save(
        _ICO,
        format="ICO",
        sizes=[(s, s) for s in _ICO_SIZES],
        append_images=renditions[:-1],
    )
    print(f"wrote {_ICO.relative_to(_ROOT)} ({len(_ICO_SIZES)} sizes)")

    _WIZARD.parent.mkdir(parents=True, exist_ok=True)
    _banner(_WIZARD_SIZE, int(_WIZARD_SIZE[0] * 0.78)).save(_WIZARD, format="BMP")
    print(f"wrote {_WIZARD.relative_to(_ROOT)} {_WIZARD_SIZE}")

    _banner(_WIZARD_SMALL_SIZE, min(_WIZARD_SMALL_SIZE)).save(_WIZARD_SMALL, format="BMP")
    print(f"wrote {_WIZARD_SMALL.relative_to(_ROOT)} {_WIZARD_SMALL_SIZE}")

    _write_msix_logos()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
