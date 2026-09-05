---
name: first-principles-audit
description: >
  Six questions, in order, for any package, file or function, ending in one
  verdict row: KEEP, MERGE-INTO <target>, SIMPLIFY, or CUT, with the
  dependents named. Use when auditing the codebase top-down or bottom-up,
  when deciding whether a module earns its place, and when writing rows for
  docs/AUDIT_LEDGER.md. A row without named dependents is not finished.
---

# First-principles audit — does this earn its place?

Anastomosis takes a raw export (C-CDA, FHIR, a tabular EHR dump), turns it
into one canonical model, renders charts and a transfer document from that
model, checks them, and delivers them to a portal or a folder with proof of
what arrived. Everything in `src/` is either on that path, teaches the tool
a new source or layout, or is the GUI and CLI over it. Anything else needs
a reason.

The audit asks the same six questions of every unit, top-down (package →
module) and bottom-up (function → line). The answers are written down, not
held in the head; the point is that a later refactor at one place can see
every connection before it cuts.

## The six questions, in this order

1. **What does it do?** One sentence, no adjectives. If the sentence needs
   "and", it may be two things.
2. **Why does it exist?** Which stage of the path needs it, or which real
   report (`#NNN`), vendor sample, or spec clause called it into being. "It
   seemed useful" is an answer; it is the answer CUT is built on.
3. **Where does it connect?** Every importer and every import. Use the
   import graph (`grimp`), coverage, and a static unused-code pass, and
   only then `rg` to confirm. grep alone misses re-exports and dynamic
   dispatch, and dynamic dispatch is what deletion breaks first.
4. **Is it crucial, or was there a simpler human alternative?** Compare with
   the Tebra equivalent if one exists, and with the stdlib. A veteran writes
   a flat function before a class, a dict before a registry, a table before
   a hierarchy. Name the alternative concretely, not "could be simpler".
5. **Would cutting it make the whole simpler?** Not just shorter: fewer
   concepts, fewer seams, fewer names to know. If the answer is yes and the
   capability survives elsewhere, the verdict is CUT or MERGE-INTO.
6. **If cut, what falls into place?** Every dependent from question 3 and
   what each does instead. This is the line that makes refactoring at one
   place safe: the cut is only allowed once every dependent has a named
   destination.

## The verdict row

```
| path | lines | what | why-it-exists | connects-to | verdict | reason | what-falls-into-place-if-cut |
```

- **KEEP** — on the path, one implementation, no simpler alternative named.
- **MERGE-INTO `<target>`** — does what `<target>` does with a different
  variable. Name the surviving function and the parameter that absorbs the
  difference.
- **SIMPLIFY** — stays, but the alternative from question 4 replaces the
  current shape. Name the shape (flat function, table, stdlib call).
- **CUT** — not on the path, or a hypothetical nobody has reported. Name
  where each dependent goes.

A row's verdict is adjudicated by driving, never by reading: delete or
stub the unit in a disposable copy and run the suite, the corpus pin and
the real export. What breaks is the dependents list, verified.

## Tests get the same six questions

Every test is classified **happy** (a real fixture through the real path),
**regression** (a filed `#NNN`, a vendor sample, a byte-level contract), or
**hypothetical** (an input nobody has produced). Hypothetical tests go with
the code they hypothesised about. The count of distinct `#NNN` guards may
not fall; the gate asserts it.

## What the audit is not

It is not a style review, and it does not rewrite. It produces the
work-list the refactor then executes one slice at a time
(`banach-tarski-refactor`). A slice does not start until its rows are
adjudicated, and its PR body cites them.

## Reference sizes, so "too big" is a measurement

| concern | here | best small reference |
|---|---|---|
| PDF text QA | 1143 lines / 5 files | 82 lines (OCRmyPDF `pdf_compare.py`) |
| archive export | 1565 | 232 (makesite) |
| FHIR mapping | ~2900, three mappers | 932 engine + 53 per resource (fhirbug) |
| splash / logo | 645 | 0 (rich, gh, cargo) |
| layout learning | 3916 | 2210 (docling's own module): honest size |
| C-CDA read + write | 5419 | 11,342 (medplum): honest size |

The last two rows are the domain's cost, not the codebase's fault. Those
get a descriptor table and a prose sweep, not a rewrite.
