---
name: quality-gate
description: >
  The mandatory pre-commit pipeline for substantive changes: scope triage,
  tests-first coverage check, the mechanical gate, a two-stage adversarial
  review (PHI/losslessness compliance first, then general quality), fixes
  with mismatch-repair re-review, and honest verdict reporting. Use before
  every commit of non-trivial code, and in full (including polymerase-review)
  before anything merges to main. Review agents report findings; they never
  hold approval authority — the orchestrator and the human do.
---

# Quality gate — nothing ships unchecked

Synthesized from this repo's battle-tested practice plus the strongest
public review skills (tests-first ordering, no-approval-authority,
confidence-rated findings, two-stage compliance-then-quality review,
parallel specialist panels). Model routing follows the `model-hierarchy`
skill: reviews run on BUILDER tier, mechanical sweeps on GRUNT tier, the
verdict call stays with the ORCHESTRATOR.

## Phase 0 — Scope triage (before reading the implementation)

- [ ] **PHI-touch triage:** does the diff touch patient-data flows, fixtures,
      logging, error paths, or anything serialized/exported? If yes, the
      Critical-PHI rules below apply and the review starts there.
- [ ] **Size check:** > ~500 changed logic lines → split the change before
      review; reviewers degrade superlinearly with diff size.
- [ ] **Tests first:** read the TESTS before the implementation. Changed
      logic with no new/changed tests blocks here — do not proceed to
      reviewing implementation until coverage exists. Test assertions must
      verify behavior (values, shapes, errors), not merely "didn't raise".
- [ ] **Existing-enzyme check:** new helpers/dependencies justified against
      what the codebase, stdlib, and declared deps already provide
      (see polymerase-review BULGE rules). Fewer, better files beat sprawl.

## Phase 1 — Mechanical gate (exact command, no interpretation)

Run exactly:

    bash tools/check.sh

with pipefail semantics; NEVER pipe gate commands through `tail`/`head` in a
way that can mask a nonzero exit (this masked two real failures in this
repo's history). The gate covers ruff (incl. bandit-S and DTZ), ruff format,
mypy --strict, pytest, and the full-tree PHI scan. Red gate = finding #1;
fix before any further phase.

## Phase 2 — Adversarial review (two stages, BUILDER tier)

Spawn the `qa-reviewer` agent on the uncommitted diff. Stage order is fixed:

**Stage 2a — compliance strand (halts the review if it fails):**
- PHI safety: no patient-derived values in logs, exception messages, console
  output, or filenames outside hardened output dirs; synthetic-data
  conventions in all fixtures/tests; `exc_tag()` on input-derived errors.
- Losslessness: no source field silently dropped; round-trips exact;
  sentinels never collide with real values (placeholder/sentinel collisions
  have been proven bugs here twice).
- Any Critical-PHI finding stops the review — surface it immediately;
  lower-severity review resumes only after it is acknowledged and fixed.

**Stage 2b — quality strand (only after 2a passes):**
- Correctness probes: boundary values, empty/None/"" inputs, unicode,
  portability (strftime %-d, paths, tz/Windows), concurrency of re-runs.
- Contract integrity: existing callers/tests/fixtures still pair with the
  change (delegate the systematic version to polymerase-review when public
  surface is touched).
- Test-gap mutation check: would an obvious mutation of the new logic
  survive the suite? If yes, the missing test is a finding.
- Architecture/efficiency: duplication, dead branches, complexity hot spots,
  dependency hygiene (new deps need a reason and a CVE glance).

**Evidence rules (every finding):** file:line, a reproduction (live probe
under /tmp — never modify the repo during review), severity, confidence
(high/medium/low), one-line suggested fix. Findings that don't reproduce are
excised. Reviewers have NO approval authority: they return findings and a
recommendation; the orchestrator decides, the human owns the merge.

## Phase 3 — Severity taxonomy

| Severity | Meaning | Effect |
| --- | --- | --- |
| **Critical-PHI** | PHI exposure risk, sentinel/placeholder corrupting clinical values, lossless-guarantee breach | Halts review; blocks commit; fix + re-review |
| **Critical** | Wrong results, data loss, broken callers/formats, security flaw, red gate | Blocks commit |
| **Warning** (should-fix) | Probable future bug, portability trap, test gap, unjustified duplication | Fix in the same change unless explicitly waived with written rationale |
| **Nit** | Style/naming/docs | Fix mechanically (GRUNT tier) or defer |

"Fix it later" in PHI-adjacent code is not a waiver — it's a Warning that
requires a tracking issue before merge.

## Phase 4 — Repair and re-review

- Apply fixes for everything at Warning and above.
- **Mismatch-repair rule:** a FRESH reviewer instance re-checks the repaired
  sites (repairs introduce errors at a known rate; the author of a fix never
  solely approves it). For small fix batches the orchestrator's own
  re-probing may stand in; for blockers, always a fresh instance.
- Add a regression test for every Critical that was found — the probe that
  proved the bug becomes the test that prevents it.

## Phase 5 — Verdict and record

- Re-run Phase 1. Commit only on ALL GATES GREEN, with the verification
  story in the commit message: what was reviewed, what was found, what was
  fixed, ACTUAL test count from gate output (never estimated).
- Report outcomes faithfully: a gate that failed and was fixed is reported
  as exactly that. Never claim green from memory.

## Status signals (subagent ↔ orchestrator)

`DONE` · `DONE_WITH_CONCERNS` (findings attached) · `NEEDS_CONTEXT` (spec
gap — orchestrator answers, never the agent guessing) · `BLOCKED` ·
`PHI_ESCALATION` (drop everything, surface to human).
