# RULES_CANDIDATES — W2 (deliver/browser, deliver/verify)

Rules found during the prose sweep that are not yet in `docs/RULES.md`.
One sentence each, with the `file:line` the sweep cut prose from.

- `VERIFYING_PRE` runs the wrong-patient banner check before the duplicate
  scan, and the duplicate scan is trusted only once that identity check has
  passed. `src/anastomosis/deliver/browser/states.py:1` (module docstring),
  `:92` (`LEGAL_TRANSITIONS` comment, pre-sweep).
- `SHARED_MACHINE_WARNING` is the exact text the CLI and GUI must show the
  operator before attaching over CDP, because loopback is reachable by any
  other local user on a shared machine.
  `src/anastomosis/deliver/browser/cdp.py:17` (module docstring, pre-sweep).

## Loose ends

(none found in this package)
