# Codex / Claude crash-safe coordination

Last reconciled: 2026-08-31 Asia/Karachi.

This PHI-free file is the write-ahead coordination record for the learned-source
audit. Read it before `HANDOFF.md` after any chat archive, model reset, rate-limit
pause, machine restart, or agent handoff. GitHub is the redundant copy; chat and
temporary directories are not recovery state.

## Exact reconciled state

- Repository: `AzalDaniel/Anastomosis`.
- Local checkout:
  `C:\Users\azald\Documents\Codex\2026-08-29\github-plugin-github-openai-curated-remote-3\work\Anastomosis`.
- Working branch: `codex/learned-source-integrity`.
- Local and remote audit head after a verified clean fast-forward:
  `71e7a51dfaa76cce9792aaccea1c84e3c41ce0c1`.
- Live GitHub `main` when reconciled:
  `28b219e1fbfac5547ee8427cb13c2e9b9ad30b3b`.
- Pull request: https://github.com/AzalDaniel/Anastomosis/pull/310.
  It is open and draft. At reconciliation it was six commits ahead and two
  commits behind `main`; do not merge it without updating from current `main`
  and rerunning the full gate.
- Tracking issue: https://github.com/AzalDaniel/Anastomosis/issues/308.
- The original Codex commit `dae8938` is recognized by GitHub as authored and
  committed by `AzalDaniel`. Later review commits are already on the branch.

## What Claude added after the original handoff

- Restored exact, normalized matches for published SOAP and C-CDA section
  vocabulary while retaining every unknown/provider/patient-like string in the
  sample-text quarantine. The e2e packgen test caught the placeholder-only
  regression and now pins both directions.
- Decomposed the learned reader/interpreter changes to satisfy the complexity
  ratchet instead of weakening its baseline.
- Restructured and tested the aggregate C-CDA probe so chart values cannot reach
  printed output by construction. The remaining CodeQL alert exposed a broader
  repository policy defect: inline SARIF suppressions were documented as a
  working control although GitHub code scanning does not dismiss them alone.
- Merged the synthetic conservation instrument in PR #311. It generated 6,144
  deterministic documents and produced separately filed findings #312-#315.
- Merged PRs #306, #307, #311, #316, and #319 into `main`.

Claude reported `bash tools/check.sh` green on PR #310's reviewed tree at 2,220
passes / 5 skips, and 2,284 passes after bringing in the then-current main. Treat
those as Claude's terminal evidence; Codex must independently rerun the final
merged-head gate before approval.

## Live merge train at reconciliation

1. PR #318, `A suppression that does nothing is worse than no suppression`:
   open, draft, mergeable at head `6277d8488185ea1e34ff0b86a4fe9372d4b3e3fc`.
   It is the prerequisite CodeQL policy/mechanism change.
2. PR #310: update from `main` only after #318 lands; resolve deliberately,
   rerun the complete gate, inspect CodeQL results, then make ready/merge.
3. PR #320, `Read the header: who wrote it, who signed it, and where`:
   open, draft, mergeable at head `9232980c6cc971f597dabd6ef206764fe70bbdbf`.
   It adds C-CDA participation extraction plus FHIR type-preserving round trip.
   It intentionally does not close #312 because two no-id constructs remain
   uncreditable by the conservation ledger.

Measured follow-up issues from the conservation instrument:

- #312: participant/author/performer/custodian/etc. coverage.
- #313: Unstructured Documents can parse successfully into an empty chart.
- #314: coded entries in unmapped sections can reach neither typed output nor
  narrative fallback.
- #315: the ledger ships in the runtime package but is not exposed to operators.
- #317: documented CodeQL suppression policy did not implement dismissal.

## Write-ahead protocol

For every material unit:

1. `git status --short --branch`; stop on unexpected local changes.
2. `git fetch --prune origin` and compare live GitHub PR metadata. Never trust a
   stale local `origin/*` ref or a pasted transcript alone.
3. Update this file and `AUDIT_LOG.md` with verified starting state.
4. Review implementation and tests; run the narrow gate, then the repository's
   complete gate where the change is merge-bound.
5. Append terminal results or the exact blocker to `AUDIT_LOG.md` before doing
   the next unit. Interrupted/partial test output is never a pass.
6. Commit with the repository owner's GitHub-recognized identity and transparent
   Codex co-authorship when Codex materially contributed:
   `Azal Daniel <166051114+AzalDaniel@users.noreply.github.com>` and
   `Co-authored-by: Codex <noreply@openai.com>`.
7. Push the branch immediately. Confirm the remote SHA through GitHub. A local
   commit is not a completed handoff.
8. Add/update the relevant issue or PR with only PHI-free aggregate evidence.

Never rewrite shared history, force-push, expose sample values/filenames, install
OCR models, mutate the VM, or weaken fail-closed behavior merely to clear a gate.
Checkpoint only `Anastomosis-Audit` before authorized VM mutation, and only
create/delete containers named `anastomosis-fhir*`.

## Resume command sequence

Run from the checkout above:

```powershell
git status --short --branch
git fetch --prune origin
git log --oneline --decorate --graph --max-count=20 --all
Get-Content -Raw docs\audits\learned-source\COORDINATION.md
Get-Content -Raw docs\audits\learned-source\AUDIT_LOG.md
```

Then query the live state of PRs #318, #310, and #320. Continue the first
unfinished merge-train item; do not repeat completed work merely because chat
history is absent.
