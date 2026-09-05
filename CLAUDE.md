# Anastomosis — read this first

Anastomosis joins an old EHR to a new one: raw export in, one canonical
structure, verified delivery out. C-CDA, FHIR R4, Practice Fusion and
Oracle EHI exports in; rendered chart PDFs, a transfer C-CDA, an offline
archive or a FHIR bundle out; an upload to the destination portal (API
where one exists, a driven browser where not) with proof of what arrived.
It learns what it has not seen before: a new tabular format from one
sample, a new page layout from a few PDFs, and, by the owner's stated
intent, a new destination portal, which today is still a manual selector
wizard. A GUI and a CLI drive the same command layer. Tebra (the owner's earlier script) does the Practice Fusion half in
1,730 flat lines and is the reference for "how a human would do it".

The five stages: **read** (`sources/`) → **model** (`core/model`) →
**render** (`reconstruct/`, `packs/`) → **check** (`qa/`) → **deliver and
verify** (`deliver/`). `packgen/` and `core/sourcelearn` are the learn
capability. Everything else is the CLI, the GUI, or the seam between them.

If a rule is not written in `docs/RULES.md`, it is not settled: decide,
and add it in the same PR. Rules marked `#NNN` were paid for by a defect.

## Never

- PHI: `docs/RULES.md` 1–2. The samples under `/tmp/kareo` are a real
  patient's records: drive them, report counts, element names, codes and
  digests, never a value or a filename.
- The corpus pin moves only deliberately (62); a gate baseline is never
  re-drawn upward (78); no test is skipped, disabled or `xfail`ed to get
  green, and the `#NNN` guard count never falls (79).
- Never `git stash` in a worktree. Never work in `/home/user/Anastomosis`
  itself; one worktree per branch under the session scratchpad.
- Breaking code to prove a test bites happens in a disposable copy
  (`cp -r src tests tools pyproject.toml`), never in the working tree.
- `codex/audit-ledger` is read-only.

## House rules (RULES.md 81–87)

Search before you write (81). A branch needs a receipt (82). A comment says
what the code cannot; docstrings 10 lines for a module, 5 for a function
(83). Copy mechanism, share knowledge (84). One concern per PR (85).
Touched, not inferred (86). Decisions go global (87). The seams:
`core/atomic.py` writes, `core/hashutil.py` hashes, `core/identity.py`
matches, `core/ccda_codes.py` turns identifiers into ids, `core/clock.py`
tells the time.

## Skills

`veteran-programmer` at the start of any session that touches code;
`first-principles-audit` for any verdict on a file; `banach-tarski-refactor`
for a slice; `thomas-to-jesus` and `potemkin-check` before any claim;
`quality-gate` before a commit; `model-hierarchy` for who does what.

## Running anything

The env prefix binds to ONE command; repeat it on each. A bare `$W` later
in the same shell has resolved to the shared checkout's editable install
before now and silently tested the wrong tree.

    PATH=/home/user/Anastomosis/.venv/bin:$PATH PYTHONPATH=<worktree>/src \
      PYTHONDONTWRITEBYTECODE=1 bash tools/check.sh

The real export, by hand, after anything on the path:
`anast migrate <dir> --out <out> --from ccda --to tebra --render ccda-standard`
→ exit 0, 2 patients, 2 rendered, 0 fail. `<dir>` is the curated pair (one
`.ccd`, one `.xml`, kept under the session scratchpad), not the raw export
root, which yields one patient on `main` too.

## GitHub, measured 2026-09

- The API quota is one bucket per account across every session, and a
  refusal reads like a missing PR. `update_pull_request`, `issue_write` and
  `list_issues` are refused first; `pull_request_read`, `issue_read`,
  `actions_*`, `create_pull_request`, `add_issue_comment` and
  `merge_pull_request` keep answering. Address things by number; never
  poll; back off instead of retrying; a draft that cannot be un-drafted
  still merges, so ask for the click.
- A PR with no checks at all is a conflict, not a queue: GitHub builds no
  merge ref, so no workflow runs. Read `mergeable_state` first. Every
  merge touches `CHANGELOG.md`, so long-lived branches conflict; land
  promptly.
- Deleting a remote branch is refused here (credential scope). Say so;
  do not retry.
- `windows-package.yml` costs an hour of `windows-latest`; it is
  path-filtered and nightly, with the filter pinned by
  `tests/unit/test_workflow_supply_chain.py`. Every push to a PR runs the
  full matrix: verify locally, push once.
