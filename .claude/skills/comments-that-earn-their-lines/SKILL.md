---
name: comments-that-earn-their-lines
description: >
  The comment and docstring policy: brief, where needed, why not what, and
  never history. Use when writing or reviewing any docstring or comment in
  src/, tests/ or tools/, when a file's prose share is over the ratchet in
  tools/prose_gate.py, and when a sweep is deleting prose and must decide
  which lines are a rule and which are a story. The gate is the enforcement;
  this skill is the judgement the gate cannot make.
---

# Comments that earn their lines

A comment costs every reader who passes it, forever. It earns that cost
by saying something the code cannot: a constraint, a surprise, a reason
the obvious way was not taken. Everything else is either the code's job
(rewrite it until it reads) or git's job (the history).

The measured state before the policy: 38% of source lines were prose, and
56 docstrings narrated the bug that produced them. The rulebook now holds
the rules those essays were protecting; the essays can go.

## The three tests a comment must pass

1. **Would the code say it if the names were better?** Then fix the names
   and delete the comment. "Don't comment bad code, rewrite it" (Kernighan
   & Pike).
2. **Is it a why, a constraint, or a surprise?** Keep. "Bytes, not text:
   newline translation once broke every Windows trust hash" is a why.
   "Loop over the sections" is not.
3. **Is it about the past?** "Used to", "before this", "previously", "until
   #NNN", "we changed", "this was", "originally": the story belongs in
   `CHANGELOG.md` and `git log`, and the *rule* it was defending belongs in
   `docs/RULES.md`. The comment itself goes. A `#NNN` may stay as a
   receipt on a rule ("boundary-anchored, never substring (#232)") but not
   as a narrative.

## The table

| Pattern | Verdict |
|---|---|
| Restates the line below | delete |
| Explains a non-obvious why, constraint, or surprise | keep, one or two lines |
| Narrates what was tried, fixed, or changed | delete; rule to `docs/RULES.md`, story to `CHANGELOG.md` |
| References the task, issue or PR as context ("added for #NNN") | delete; the commit message holds it |
| Cites a spec clause, LOINC, template OID, or vendor quirk the code depends on | keep |
| Module docstring over 10 lines | cut to what a new reader needs to pick the right file |
| Function docstring over 5 lines that is not a contract | cut |
| A contract: input shape, what raises, what is never emitted | keep, and it may run long |
| Test docstring that repeats the test name | delete |
| `TODO`, `FIXME`, `XXX` | delete or file an issue; the tree is not a tracker |

## The caps (RULES.md 83), enforced by `tools/prose_gate.py`

- Module docstring ≤ 10 lines. Function and class docstring ≤ 5 lines,
  unless it states a contract and says so.
- No history words in any docstring or comment.
- Prose share per file, per package, and repo-wide is a ratchet against a
  checked-in baseline: it may shrink, never grow.
- Exemptions live in an allowlist with a one-line reason each. An
  exemption without a reason is a failure.

## Where the story lives instead

- **The rule** → `docs/RULES.md`, one sentence a reviewer can check a diff
  against, with the `#NNN` receipt.
- **The change** → `CHANGELOG.md` in the release it shipped in, and the
  commit message.
- **The evidence** → the PR body and the issue.
- **The contract** → the docstring, briefly, or the type signature.

## The sweep, so a rule is never deleted with its essay

Before deleting a docstring longer than the cap, read it once for a rule
that lives nowhere else. If you find one, add it to `docs/RULES.md` in the
same commit, then delete the prose. `tools/ast_equal.py` proves the sweep
touched nothing but prose: the module's AST with docstrings stripped is
byte-identical before and after. A non-empty AST diff means the sweep is
wrong, not the code.

## In tests

A test's name is its docstring: `test_a_bundle_is_byte_identical_across_two_
independent_loads` needs no paragraph. The `#NNN` goes in the name or a
one-line comment so the guard count can find it.
