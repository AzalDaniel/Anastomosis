# RULES_CANDIDATES.md — worker W9 (packgen, qa, cli_commands)

One sentence per candidate, with the `file:line` it was pulled from. The
prose at that site was cut to this sentence in place.

1. `cli_commands/__init__.py:1` — No `cli_commands` module imports
   `anastomosis.gui`, and importing `gui` never imports `cli_commands`
   (peer-frontend boundary), pinned by `tests/unit/test_import_boundaries.py`.
2. `cli_commands/__init__.py:1` — A command module resolves `console`,
   `_glyphs`, and the Playwright-attach seams (`_make_destination`,
   `_make_validator`) late, via a function-scope `from anastomosis import cli
   as _cli`, never at module import time, so `cli`'s monkeypatch seams still
   take effect.

3. `cli_commands/_options.py:3` — A shared `Annotated[...]` option alias is
   for options that are literally the same option; a command needing
   different wording for a flag (`migrate`'s `--from`, the delivery pair's
   own `--out` phrasing) declares its own alias rather than reusing this one.

4. `qa/checks.py:309 UnattributedVitalsCheck` — This check is deliberately
   silent about a non-vital observation with no `encounter_id` (e.g.
   smoking status): a fact about the patient rather than a visit is not a
   defect, and flagging it would put a finding on every chart of every
   patient ever asked about tobacco. A vital-with-no-encounter finding is
   reported once per chart of the record, not once total, because no single
   document owns the missing link.
5. `qa/checks.py:420 RecordCoverageCheck` — Its findings name only kinds and
   counts, never a diagnosis or drug value, because a coverage finding can
   travel into a run-level summary outside the hardened QA-report directory
   that RULES.md 4 exempts (unlike `DataIntegrityCheck`, whose findings stay
   inside `qa_report.json` and may quote the value that failed to match).

## Loose ends
