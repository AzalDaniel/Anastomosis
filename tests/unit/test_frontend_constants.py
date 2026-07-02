"""Pin frontend/backend constant parity (Codex re-audit follow-up).

The browser UI mirrors a handful of backend constants by hand
(``DEFAULT_MAX_ATTEMPTS`` in ``gui/web/console.js`` mirrors
``core/upload_command.py``). Until the constants are generated or served
from one Python-canonical source (the planned ``gui_config()`` follow-up),
this test is the drift alarm: if either side changes without the other,
the suite fails with a message naming both files.
"""

from __future__ import annotations

import re
from pathlib import Path

from anastomosis.core.upload_command import DEFAULT_MAX_ATTEMPTS

_CONSOLE_JS = (
    Path(__file__).resolve().parents[2] / "src" / "anastomosis" / "gui" / "web" / "console.js"
)


def test_frontend_backend_retry_constants_do_not_drift() -> None:
    """The JS console's default retry budget must equal the Python
    ``DEFAULT_MAX_ATTEMPTS`` — the value the upload engine actually enforces.
    A drift here silently shows the operator a different retry promise than
    the backend delivers.
    """
    source = _CONSOLE_JS.read_text(encoding="utf-8")
    match = re.search(r"^const DEFAULT_MAX_ATTEMPTS\s*=\s*(\d+)\s*;", source, re.MULTILINE)
    assert match is not None, (
        f"could not find 'const DEFAULT_MAX_ATTEMPTS = <int>;' in {_CONSOLE_JS} — "
        "if the constant moved or is now served via gui_config(), update this test "
        "to assert against the new source of truth."
    )
    js_value = int(match.group(1))
    assert js_value == DEFAULT_MAX_ATTEMPTS, (
        f"frontend/backend retry-budget drift: console.js has {js_value}, "
        f"core.upload_command.DEFAULT_MAX_ATTEMPTS is {DEFAULT_MAX_ATTEMPTS}. "
        "Change both together (or land the gui_config()/generated-constants fix)."
    )
