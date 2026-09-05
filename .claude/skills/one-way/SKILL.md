---
name: one-way
description: >
  One problem, one solution, one place. Use before writing any helper,
  parser, hasher, writer, id recipe or mapping; when two functions in two
  packages share a name or a shape; and when a review finds the same
  decision made two ways. Isomorphic problems get isomorphic solutions, and
  a guard test fails when a second implementation appears. The HL7 II story
  (#404, #405, #412, #413) is the worked example.
---

# One way — isomorphic problem, isomorphic solution

Every "the same thing five ways" in this codebase was written by a session
that could not see the other four. The fix is not memory; it is a habit
before writing and a test after.

## The worked example

An HL7 `II` identifier is a pair `(root, extension)`. Four places turned
that pair into an id, each by hand: patient, encounter, organization, and
clinical facts. They disagreed on whether a bare root names the instance,
on how to escape a `:` inside a root, and on what to do with no id at all.
Five issues later (#404, #405, #408, #412, #413) one function exists,
`identity_from_ii` in `core/ccda_codes.py`, taking the kind, the pair, a
fallback, and one stated argument: `bare_root_names_the_instance`. Every
caller passes it with a reason. A test fails if `quote(` and `uuid5(`
co-occur outside that module. Nothing about the four copies was wrong on
its own; they were wrong together.

## Before writing

- [ ] **Search by three names.** What it does (`hash`, `digest`), what it
      returns (`sha256`), what neighbours call it (`fingerprint`). Search
      `src/` and `tests/`. Write what you searched in the PR body.
- [ ] **Search by shape.** A function taking `(path) -> str` that reads
      bytes and hashes them exists three times here today; the signature
      finds what the name misses.
- [ ] **Check the seam list.** `core/atomic.py` writes files;
      `core/hashutil.py` hashes; `core/identity.py` matches names and
      dates; `core/ccda_codes.py` turns identifiers into ids;
      `core/clock.py` tells the time. If your need is one of those, the
      answer is an import, not a function.
- [ ] **Same name in two packages is a smell, not a coincidence.** A
      `_digest` in `qa/` and a `_digest` in `deliver/verify/` is one
      function with two homes. Merge before adding a third.

## When two exist

Prefer the one with the guard test, then the one with more callers, then
the flatter one. Move callers with expand-contract: add the parameter the
survivor needs, migrate every caller, delete the loser in the same PR.
Never leave both alive "for now"; "for now" is how the third one arrives.

The difference between the two is usually one variable. That variable
becomes an argument with a name and a stated default, as
`bare_root_names_the_instance` did. If the difference cannot be named, the
two are not the same thing and stay apart; say so in the ledger.

## The guard

Every consolidation ships a test that fails when a second implementation
appears, in the shape #413 used:

```python
def test_only_one_module_encodes_a_compound_identifier():
    # quote( and uuid5( together are the recipe; only ccda_codes.py may hold it
```

Grep-shaped guards are blunt on purpose: a false positive costs a
reviewer one look, a false negative costs five issues. Refine the pattern
only when a false positive is driven, never pre-emptively.

## Decisions go global

A decision made for one adapter is made for all of them. If the C-CDA
reader keeps a source id whole, the FHIR reader and the tabular readers do
too, in the same PR or the next one with an issue naming it. Two adapters
with opposite policies on the same question are a #405 waiting to be
filed.

## Where it is written down

`docs/RULES.md` names the one implementation for each seam. When a new
seam appears, it gets a rule there in the PR that creates it. If a rule is
not written there, it is not settled.
