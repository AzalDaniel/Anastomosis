---
name: potemkin-check
description: >
  Detect work that looks done but is not: stub returns, vacuous tests,
  hardcoded happy paths, swallowed errors, controls wired to nothing, dead
  configuration, and spaghetti that passes while nobody can say why. Run
  before marking any task complete and before any claim of "it works"
  reaches the user or a PR body. Composes with quality-gate (this is its
  claim-integrity strand) and with thomas-to-jesus (which governs HOW each
  check here is evidenced).
---

# Potemkin check — the village must have houses behind the facades

Agents fail in a characteristic direction: toward output that *reads* as
done. The published record is blunt about it — manual validation found 71.5%
of "successful" AI-written exploit PoCs never touched their target; frontier
models rewrite tests, hardcode expected outputs, and monkey-patch assertions
when the visible check is easier to satisfy than the real objective (METR's
MALT dataset, NIST/CAISI's eval-cheating background, Cursor's SWE-bench
container-trust finding); and "Potemkin understanding" (arXiv 2506.21521)
shows a model can define a concept correctly 94% of the time and still fail
to apply it — the agent that explains the fix perfectly has not necessarily
made it. This repo's own history says the same thing in its own vocabulary:
the C-CDA audit's founding observation was that "it parsed" counted only
survivors, and a count of survivors looks identical whether the loss was
zero or total.

So: no claim ships on plausibility. Every one is checked against the pattern
list, in order.

## The patterns (what to look for)

1. **Stub behind a working face** — a function that returns a canned value,
   an implementation that special-cases exactly the fixture, a demo path
   hard-wired to the happy case.
2. **Vacuous test** — asserts nothing that could fail: `assert result is not
   None`, asserting on the mock it configured, a "didn't raise" pass. The
   misguidance effect is real (arXiv 2607.22883): buggy code in context
   steers generated tests into validating the bug.
3. **Swallowed failure** — an except/catch that logs (or doesn't) and
   reports success; an error branch nothing has ever entered.
4. **Unwired surface** — a CLI flag, GUI control, or config key that changes
   no observable behavior. The UI accepting input is not the backend
   receiving it.
5. **False accounting** — a summary/report/count derived FROM the thing it
   is supposed to check, so it balances no matter what was lost (the exact
   shape `ledger.py`'s `_offered` refuses).
6. **Doc-code divergence** — README/MAPPING.md/help text describing behavior
   the code does not have. A lying doc is a Potemkin facade with a byline.
7. **Spaghetti that works** — duplicated logic drifting apart, a stdlib
   re-implementation, an abstraction layer with one caller. Not a lie, but
   the soil lies grow in; flag it even when behavior is correct.

## The checks (in order, each with evidence per thomas-to-jesus)

- [ ] **Claim inventory.** List every claim the completion message or PR
      body will make. Each gets a verdict below; an unlisted claim may not
      be made.
- [ ] **Claim-to-code trace.** For each claim, name the file:line that
      fulfils it. Cannot point to it → not done.
- [ ] **Delete-and-fail.** For each new behavior, break the implementation
      (in a COPY of the tree, never the working tree — `git checkout --`
      has destroyed real work here before) and show the guarding test go
      red. A test that survives its implementation's deletion is decoration;
      rewrite it. Full mutation testing is reserved for core clinical logic
      (field mapping, identity, conservation) where its cost pays.
- [ ] **Assertion audit.** New/changed tests assert VALUES, shapes, and
      error types — grep for assertion-free passes and mock-testing-mock.
- [ ] **Error-path touch.** Induce one real failure through each new error
      branch and observe the loud refusal. Reading the code is not entering
      the branch.
- [ ] **Unhappy input.** Exercise at least one malformed/hostile input per
      surface (the corpus generator and the hostile-document probes exist
      for exactly this).
- [ ] **Wiring sweep.** Every advertised flag/control/option is exercised
      end-to-end and visibly changes output. A no-op control is reported,
      never shipped silently.
- [ ] **Seam check.** The proof ran through a seam the change cannot itself
      edit — the real CLI, the shipped page in a real browser, the produced
      file's bytes — not only the unit seam beside the code.
- [ ] **Verdict table.** PASS/FAIL per claim with the evidence artifact
      (command + output excerpt, hash, or screenshot) attached. Prose
      summaries are not verdicts.

## Standing rule

A finding here is never resolved by weakening the check that found it. The
never-rules hold: no test skipped, disabled, or quarantined to get green; no
baseline re-drawn to admit a failure; no count derived from the columns it
is meant to audit.
