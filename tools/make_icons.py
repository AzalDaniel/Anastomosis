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
_ICO = _ROOT / "assets" / "icon" / "icon.ico"
_WIZARD = _ROOT / "assets" / "installer" / "wizard.bmp"
_WIZARD_SMALL = _ROOT / "assets" / "installer" / "wizard-small.bmp"

# Explorer/taskbar/Start-menu coverage per Windows iconography guidance.
_ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

# Inno Setup 6 wizard image baseline sizes (Inno scales for DPI from these).
_WIZARD_SIZE = (164, 314)
_WIZARD_SMALL_SIZE = (55, 58)

# The app's dark warm ground — matches the SVG tile and the GUI wallpaper.
_GROUND = (0x17, 0x13, 0x10)


def _render(size: int) -> PILImage:
    import cairosvg  # type: ignore[import-untyped]
    from PIL import Image

    png = cairosvg.svg2png(url=str(_SVG), output_width=size, output_height=size)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def _flatten(image: PILImage) -> PILImage:
    """BMP has no alpha channel worth trusting — composite onto the ground."""
    from PIL import Image

    base = Image.new("RGB", image.size, _GROUND)
    base.paste(image, mask=image.split()[3])
    return base


def _banner(size: tuple[int, int], mark_px: int) -> PILImage:
    from PIL import Image

    mark = _render(mark_px)
    canvas = Image.new("RGBA", size, (*_GROUND, 255))
    canvas.paste(mark, ((size[0] - mark.width) // 2, (size[1] - mark.height) // 2), mark)
    return _flatten(canvas)


def main() -> int:
    if not _SVG.is_file():
        print(f"missing SVG master: {_SVG}", file=sys.stderr)
        return 2

    renditions = [_render(s) for s in _ICO_SIZES]
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
