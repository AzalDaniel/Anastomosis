"""Preflight the local gate's optional-but-installed dependencies.

``tools/check.sh`` presents itself as the full local gate, but a fresh
``[dev]`` venv has Playwright installed *without* a Chromium download —
and the doctor/e2e tests then fail deep inside pytest with
``bundled Chromium: executable not found at resolved path``, which reads
like a product bug rather than a one-command setup step (Codex re-audit
P2). This preflight turns that failure mode into one actionable line
BEFORE pytest runs.

Exit contract:

* playwright not installed at all -> exit 0 (a minimal install is a
  supported gate configuration: the render-dependent tests importorskip);
* playwright installed AND its Chromium executable present -> exit 0;
* playwright installed but Chromium missing -> exit 1 with the exact
  command to run.

No browser is launched — only the executable path is checked, so the
preflight costs milliseconds.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        # Minimal install: render-dependent tests importorskip cleanly.
        return 0

    with sync_playwright() as pw:
        executable = Path(pw.chromium.executable_path)
    if executable.exists():
        return 0

    sys.stderr.write(
        "preflight: Playwright is installed but its Chromium browser is not.\n"
        f"  expected executable: {executable}\n"
        "  fix: playwright install chromium\n"
        "The gate would otherwise fail inside pytest with 'bundled Chromium: "
        "executable not found at resolved path'.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
