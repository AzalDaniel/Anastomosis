# Working notes for a session on this repository

The doctrine lives in `.claude/skills/` — `thomas-to-jesus` (nothing is
reported that was not touched), `potemkin-check` (what to check before saying
done), `quality-gate`, `polymerase-review`, `model-hierarchy`. Read them.

This file is the operating detail those skills assume: the things sessions
here keep rediscovering the expensive way. Every claim below was measured,
and says when.

## The GitHub API budget is shared, and it is the usual reason a session stalls

One account's quota covers every Claude Code session and every repository at
once. A second session running beside you is enough to exhaust it, and the
failure does not look like a quota failure — it looks like a tool that cannot
find a pull request that plainly exists.

Measured across 2026-09-03/04, over more than forty minutes of continuous
refusal: `update_pull_request` and `list_issues` returned *"API rate limit
already exceeded for user ID …"* on every attempt, while in the same minutes
`pull_request_read`, `issue_read`, `actions_list`, `actions_get`,
`create_pull_request`, `add_issue_comment` and `merge_pull_request` all
answered normally. The buckets are not one bucket. Prefer the second set;
reach for the first only when nothing else will do.

- **Never poll.** `subscribe_pr_activity` wakes the session on check-suite
  completion, comments and reviews. One check-run read per wake is enough; a
  loop of status reads is quota another session needed.
- **Back off, do not retry.** A refused write retried immediately is a refused
  write twice. Schedule a check-in and say what is blocked.
- **A draft pull request cannot be merged, and clearing the draft flag is one
  of the calls that gets refused.** When that happens, the merge itself still
  works — say so and ask for the click, rather than spending the budget.
- Address things directly (`pull_request_read` with a number, `issue_read`
  with a number) instead of searching for them.

## A pull request with no checks at all is usually a conflict, not a queue

GitHub cannot build a merge ref for a conflicting pull request, so it creates
no `pull_request` workflow run — no run, no checks, no failure, nothing.
Driven on #377 and #378: both sat with zero check runs for twenty minutes
after a push, both reported `mergeable_state: dirty`, and CI started within
seconds of the conflict being resolved. Check `mergeable_state` before
concluding anything about CI.

Long-lived branches earn this by sitting: every merge to `main` touches
`CHANGELOG.md`, so every open branch conflicts the moment another one lands.
Land work promptly, and expect to merge `main` into each remaining branch in
turn.

## CI is not free, and the installer lane is the expensive one

`windows-package.yml` builds two Nuitka standalone executables and smoke-tests
the installer: 63 minutes of `windows-latest` on run 33798695980. It no longer
runs on every source merge — the path filter watches the packaging scripts,
the workflow and the dependency set; a nightly build is the canary; a release
tag still builds unconditionally. `tests/unit/test_workflow_supply_chain.py`
pins that, and re-adding `src/anastomosis/**` is a decision to make there
deliberately.

Every push to a pull request runs the full matrix. Batch them: verify locally,
push once.

## Running anything

The env prefix binds to ONE command. Repeat it on each; a bare `$W` in a later
command in the same shell has resolved to the shared checkout's editable
install before now, which silently tests the wrong tree.

    PATH=/home/user/Anastomosis/.venv/bin:$PATH PYTHONPATH=<worktree>/src \
      PYTHONDONTWRITEBYTECODE=1 bash tools/check.sh

Read the exit code unpiped — a `| tail` has laundered a failure here before.

The C-CDA corpus reading is the repository's sharpest instrument and its
digest is a pin:

    python tools/ccda_corpus.py --ledger --count 6144 --seed 7 | sha256sum

Record it before and after any change to `sources/ccda/`. A moved pin is a
decision to explain — diff the two readings and say which rows moved and why —
never a surprise to absorb.

## Worktrees

One worktree per branch, under the session scratchpad. Never work in
`/home/user/Anastomosis` itself; agents share it.

- **Never `git stash` in a worktree.** The stash list belongs to the
  repository, not the worktree, and `pop` has landed another branch's work
  into a clean tree here.
- Breaking the code to prove a test bites happens in a disposable full copy
  (`cp -r src tests tools pyproject.toml`), never in the working tree, and
  never by editing a file an agent is also holding.
- Deleting a remote branch is refused in this environment: the credential
  scope hangs up on a delete-ref push, twice, reproducibly. Merged branches
  are normally auto-deleted; if one lingers, say so rather than retrying.

## PHI

The sample exports under `/tmp/kareo` are a real patient's records. Drive
them — they are the only end-to-end evidence that means anything — and report
counts, LOINC codes, element names, template OIDs, exit codes and digests.
Never a value, never a filename, not in a log, a message, a test name, an
issue or a commit. The refusal messages this repository writes are the model:
"document 1 of 2, in filename order".
