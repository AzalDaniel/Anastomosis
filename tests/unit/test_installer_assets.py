"""Inno Setup stretches the 100 % wizard art when that is all it is given, so
each rendition must exist and be the size its name claims."""

from __future__ import annotations

import re
import struct
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_ISS = _ROOT / "packaging" / "anastomosis.iss"

_DIRECTIVES = ("WizardImageFile", "WizardSmallImageFile")


def _listed(directive: str) -> list[Path]:
    text = _ISS.read_text(encoding="utf-8")
    match = re.search(rf"^{directive}=(.+)$", text, re.MULTILINE)
    assert match, f"{directive} is not set in {_ISS.name}"
    return [_ROOT / entry.strip().replace("\\", "/") for entry in match.group(1).split(",")]


def _bmp_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:26]
    width, height = struct.unpack("<ii", header[18:26])
    return width, abs(height)


def test_every_wizard_image_the_installer_names_is_on_disk() -> None:
    for directive in _DIRECTIVES:
        listed = _listed(directive)
        assert listed, directive
        missing = [p.name for p in listed if not p.is_file()]
        assert not missing, f"{directive} names files that do not exist: {missing}"


def test_the_wizard_ladder_covers_every_dpi_step_at_its_own_size() -> None:
    for directive, base in ((_DIRECTIVES[0], (164, 314)), (_DIRECTIVES[1], (55, 58))):
        listed = _listed(directive)
        assert _bmp_size(listed[0]) == base, f"{listed[0].name} is not the 100% rendition"
        for path in listed[1:]:
            percent = int(re.search(r"-(\d+)\.bmp$", path.name).group(1))
            expected = (round(base[0] * percent / 100), round(base[1] * percent / 100))
            assert _bmp_size(path) == expected, f"{path.name} is {_bmp_size(path)}, want {expected}"
        assert len(listed) >= 4, f"{directive} offers only {len(listed)} rendition(s)"
