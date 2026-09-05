---
name: no-edge-case-without-a-sample
description: >
  A defensive branch, a configuration flag, a fallback, or a test exists
  because of a filed report (#NNN), a real vendor sample, or a spec clause,
  and says which. Use when writing any `if`, `try`, or `else` that handles
  an input the happy path does not; when reviewing a diff that adds one;
  when classifying tests as happy, regression or hypothetical; and when
  deciding whether a branch survives an audit. Speculation is cut.
---

# No edge case without a sample

Every special case is a permanent tax on the whole system (Ousterhout).
The model's habit is to pay it in advance: matched LLM solutions carry
more branches than human ones, from validation nobody asked for (arXiv
2501.16857), and the catalogued LLM bug types include both "prompt-biased
code" and "missing corner case" (arXiv 2403.08937). It guards the imagined
input and misses the real one. This repo's own reading: about 60% of the
tabular-source tests trap inputs no vendor has produced, while the Kareo
export found four defects nobody had guessed at (#400, #401, #402, #378).

A guard is not free even when it is right. It is a branch to read, a test
to keep green, and a place for the next session to add a fifth variant.

## The receipt

Every branch off the happy path names one of:

- **A filed report.** `#NNN` in the guard test's name or a one-line
  comment. The issue holds the sample's shape (never its bytes).
- **A vendor sample.** A committed synthetic fixture under
  `tests/fixtures/`, or a driven run against the owner's real export
  reported by counts and digests.
- **A spec clause.** The C-CDA template, the FHIR element cardinality, the
  HL7 `nullFlavor` table, the vendor's schema brief. Cite it.

No receipt, no branch. Write the happy path; when the real input arrives,
the receipt arrives with it, and the branch is then a regression guard
rather than a guess.

## What this does not forbid

- **Refusing loudly.** "This is not a `ClinicalDocument`" raising
  `ValueError` is not an edge case; it is the contract's boundary, and it
  is one line.
- **Hostile fixtures for real threats.** Path climb-out in a manifest, a
  redirect re-attaching a bearer token, a pack with a changed hash. These
  have receipts in `docs/RULES.md` and stay.
- **A spec's stated variants.** If the spec says a section may repeat, the
  parser handles a repeat. The spec is the sample.

## Classifying a test

| class | evidence | fate |
|---|---|---|
| happy | a real fixture through the real path, asserting values | keep |
| regression | a `#NNN`, a vendor sample, or a byte-level output contract | keep; the count may not fall |
| hypothetical | an input nobody has produced, asserting on a guess | goes with the code it hypothesised about |

A test that only proves a guard exists, on an input the guard invented, is
decoration for the guard. Both go in the same PR.

## Reviewing a diff that adds a branch

- [ ] Which real input reaches this branch? Name it.
- [ ] What does the happy path do with that input today? If the answer is
      "raises with a clear message", the branch may be unnecessary.
- [ ] Could the interface be shaped so the input cannot arrive? Prefer
      that (define the error out of existence).
- [ ] Is the receipt in the test name or a one-line comment?
- [ ] Does an existing branch elsewhere already handle the same shape?
      Then this is a `one-way` problem, not an edge case.

## The line that gets written in the PR

"Branches added: N, each with a receipt" or "no branches added". A branch
without a receipt is a review finding, not a nit.
