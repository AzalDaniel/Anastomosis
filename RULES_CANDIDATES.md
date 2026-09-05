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

3. `cli_commands/_options.py:9` — A shared `Annotated[...]` option alias is
   for options that are literally the same option; a command needing
   different wording for a flag (`migrate`'s `--from`, the delivery pair's
   own `--out` phrasing) declares its own alias rather than reusing this one.

## Loose ends
