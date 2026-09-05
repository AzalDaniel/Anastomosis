---
name: banach-tarski-refactor
description: >
  Halve the codebase and prove the whole is still equal: golden-master
  snapshots first, one slice per PR, structural cuts before cheap ones,
  metrics before and after, the real export driven after every slice, and
  stop conditions that are obeyed. Use for any reduction, consolidation or
  de-bloat work on src/, tests/ or the GUI, and as the loop each slice of
  the refactor program runs. Adapted from goal-sloc's self-audit and
  reduction order and addyosmani's code-simplification checklist.
---

# Banach–Tarski refactor — halved, yet equal

The whole must be equal to the prior: every command, flag, default, GUI
control, deliverable, source and destination works exactly as before, or
the slice does not merge. Lines are the proxy; behaviour is the goal. Left
alone you will move the number the cheap way and call it progress
(goal-sloc); the one prior "de-bloat" commit here netted −16 lines.

## 0. Before cutting anything

- [ ] **The net exists and was proven.** `tools/snapshot.py` captures every
      deliverable of every real input under `SOURCE_DATE_EPOCH`, plus the
      CLI surface from Typer. Run twice on one commit, the diff was empty;
      a non-empty diff there means the normaliser is wrong, not the code.
- [ ] **The baseline is written down** before the first edit: code lines
      and prose lines separately, complexity blocks, test lines, `#NNN`
      guard count, corpus pin digest.
- [ ] **The floor is known.** Vendored XSLT, YAML, JSON, HTML and CSS;
      layout inference and C-CDA parsing near their honest size. Do not
      sell the floor as a target.
- [ ] **Dead means three tools agree.** Coverage-zero ∩ static-unused ∩
      import-graph-unreachable. grep confirms; grep never decides.
- [ ] **The ledger rows are adjudicated** with named dependents
      (`first-principles-audit`).

## 1. The honest order (risk rises as you go down)

1. Dead code, three-tool verified.
2. Placeholder subsystems: plumbed features that do nothing.
3. Prose that restates or narrates, with `tools/ast_equal.py` proving
   nothing but prose moved. Legitimate hygiene, never a strategy.
4. Genuine duplication → one function, the difference a named argument
   (`one-way`). Often line-neutral; do it for clarity.
5. Hypothetical branches and their tests (`no-edge-case-without-a-sample`).
6. A hand-written mapper → a declarative table the walker reads.
7. Architectural collapse: a redundant layer, a class per level, a
   registry with one entry. Line-neutral; beware the god object.
8. Delegate to the stdlib or a dependency already in `pyproject.toml`.

Past 8 the only lever is scope, and scope is the owner's call. Say so
with the numbers. Never delete a capability quietly.

## 2. Gaming versus honesty

| Move | Verdict |
|---|---|
| Edit the counter, exclude paths, widen the formatter, strip blanks | gaming |
| Re-baseline a gate upward; skip, xfail or quarantine a test | gaming, and a never-rule |
| Delete a feature, a flag, an alias, a source, a destination | scope cut; owner decides |
| Delete dead, placeholder, or duplicate code | legitimate |
| Trim restating prose | legitimate hygiene; if it dominates the slice, find structural work |
| Replace hand-rolled code with the stdlib | legitimate when it is the better engineering |

**Self-audit, every slice:** what share was structural (1, 2, 4–8) and what
share cheap (3, formatting)? The split goes in the PR body. If cheap
dominates while structural work remained, the slice is gaming you.

## 3. Per slice, in this order, every one read unpiped

1. `bash tools/check.sh` → exit 0; complexity and prose baselines
   regenerated in the same commit and strictly no larger.
2. Corpus pin unchanged, or the PR says why it moved and which rows.
3. `python tools/snapshot.py` reports PASSED: every deliverable and the CLI surface match the committed baseline.
4. The real export by hand: both Kareo documents → exit 0, 2 patients,
   2 rendered, 0 fail. The tabular fixture → exit 0.
5. `pytest tests/gui_e2e -m gui_e2e` in real Chromium when the GUI or its
   backend moved.
6. Mutation: break each consolidation's survivor in a disposable copy and
   watch the guard go red, by explicit node id, never `-k`.
7. In the PR body: code lines, prose lines, complexity blocks, test lines,
   `#NNN` count, before and after, and the structural/cheap split.

## 4. Stop when

- A snapshot byte changed and cannot be explained in one sentence.
- The corpus pin moved unintended, or the `#NNN` guard count fell.
- You want to skip a test, add an `xfail`, or raise a baseline.
- The real export is not exit 0 / 2 patients / 2 rendered / 0 fail.
- The delta is under 200 lines: churn; fold it into a neighbour.
- The slice cannot be finished and verified this session: a
  half-collapsed layer is worse than none.

Stopping is a report, not a failure; "the structural well is dry here" is
a finding the owner needs.

## 5. Shape of a slice PR

One concern; under roughly 400 hand-edited lines, or a script in `tools/`
above 500 mechanical ones. Refactor and feature never share a PR. Deleted
code takes its tests with it. The body cites the ledger rows, the metrics,
the structural split, and the verification ledger with exit codes.
