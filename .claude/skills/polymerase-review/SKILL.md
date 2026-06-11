---
name: polymerase-review
description: >
  Two-strand "DNA polymerase" code review: the leading strand walks the NEW
  change forward (intent → implementation), the lagging strand walks BACKWARD
  from the EXISTING codebase in fragments (retro-compatibility), with
  proofreading and mismatch-repair passes. Use before committing any
  substantive change, especially ones that touch existing public surface
  (signatures, schemas, file formats, fixtures, CLI). Complements — does not
  replace — the quality-gate skill.
---

# Polymerase review — does the change zip shut?

A change and its codebase are two strands of one molecule. Review =
replication fidelity: every tooth of the zipper must base-pair, in BOTH
directions. A mismatch left in place propagates to every future copy.

## 0. Helicase — unzip the change surface

Before judging anything, enumerate the **template strand**: every public
tooth the diff touches.

- `git diff HEAD --stat` and `git diff HEAD` — the raw change.
- List every touched: exported symbol, function signature, pydantic
  model/field, serialized format (file layout, JSON/TSV shape, FHIR/CDA
  element), CLI flag, fixture file, config key, documented claim
  (README/PLAN/docstring promises).
- This list IS the review scope. Anything not on it doesn't get reviewed
  twice; everything on it gets reviewed from both directions.

## 1. Leading strand — forward, continuous (intent → code)

Walk the NEW code once, in reading order, pairing each base:

- **Requirement→code pairing (A=T):** for each requirement in the spec /
  issue / plan item, point to the exact implementation site. An *unpaired
  requirement* = missing feature (MISMATCH).
- **Code→requirement pairing (G≡C):** for each new function/branch/file,
  point to the requirement that demanded it. *Unpaired code* = unrequested
  complexity (BULGE) — the spaghetti seed. Delete or justify.
- **Existing-enzyme check:** before accepting any new helper, search the
  codebase AND the dependency set for an existing implementation
  (`grep -ri <concept> src/`, check core utils, stdlib, declared deps).
  Reimplementing an existing enzyme is a BULGE even when the code is good.
- **Template fidelity:** new code matches the strand it extends — naming,
  error style, logging discipline, typing strictness of the surrounding
  module. Style drift is a NICK.

## 2. Lagging strand — backward, in Okazaki fragments (codebase → change)

Now reverse direction: start from what ALREADY exists and check that each
fragment still base-pairs against the new strand. Work in discrete
fragments — one per existing dependent — not one continuous skim:

- **Find the dependents:** for every touched tooth from step 0, grep the
  whole repo for its users: callers, subclasses, tests, fixtures, templates,
  docs, CI config (`grep -rn <symbol> src/ tests/ docs/ .github/`).
- **Per fragment, verify pairing:** does the old call-site still typecheck,
  still mean the same thing, still parse the same bytes? Signature
  compatibility is necessary but not sufficient — check *semantic* pairing
  (a renamed sentinel, a tightened regex, a changed default all break
  pairing silently).
- **Serialized-format fragments:** anything persisted or exchanged (fixtures,
  archives, bundles, reports) gets a read-back check: can yesterday's output
  still be consumed today (backward), and can today's output be consumed by
  yesterday's documented contract (forward)?
- **Fragment ledger:** record each fragment as PAIRED or MISMATCH. No
  fragment may be skipped because it "obviously still works" — Okazaki
  fragments are small precisely so none is skipped.

## 3. Proofreading — 3'→5' exonuclease (check the checker)

Immediately re-verify your own findings before reporting:

- Every MISMATCH must reproduce with a concrete probe (a failing command,
  test, or /tmp script). A finding that does not reproduce is **excised** —
  false findings cost as much as missed ones.
- Every PAIRED verdict on a high-risk tooth (data formats, money, PHI,
  dates) gets one spot-check probe, not just inspection.

## 4. Mismatch repair — independent second pass on the repairs

After fixes are applied, a **different agent instance** (not the one that
wrote the fixes) re-reviews ONLY the repaired sites plus their lagging-strand
fragments. Repair introduces errors at a known rate; the repair enzyme never
checks its own work.

## 5. Ligase — seal the backbone

Only when both strands report zero MISMATCH: run the full repo gate
(`bash tools/check.sh` here — pipefail, never piped through tail) and commit.
The gate seals nicks; it does not excuse mismatches.

## Finding taxonomy

| Label | Biology | Meaning | Action |
| --- | --- | --- | --- |
| **MISMATCH** | non-complementary base | Broken pairing: bug, regression, broken caller/format/contract | Blocks commit; fix + re-pair |
| **BULGE** | unpaired insertion | Code with no requirement: duplication, dead branch, reinvented enzyme, speculative feature | Remove or justify in writing |
| **NICK** | backbone gap | Style/format/doc gap the gate or a grunt-tier sweep can seal | Fix mechanically |
| **SILENT** | synonymous substitution | Intentional behavior change that pairs with an updated requirement | Allowed; must be named in the commit message |

## Exit criteria

- Strand ledgers complete: every tooth from step 0 has a leading-strand AND
  lagging-strand verdict.
- Zero MISMATCH; every BULGE removed or justified; NICKs sealed.
- Proofreading excisions noted; mismatch-repair pass done if repairs were
  made; gate green.
