# What goes wrong in AI-written code, and the rules that stop it

Six research lines, sixty-odd sources. Only findings marked VERIFIED made it in. Where a number is quoted, the scout read it in the primary source or a search extract of it; nothing here rests on a blog's say-so alone. This repo's own driven evidence is cited as `(this repo)`.

## The problems

1. **The same thing gets written several ways.** Copy-pasted lines rose from 8% to 16% of all changes across 623M changes (2021–2026) while refactored lines fell from 25% to 4%; blocks of five or more duplicated lines grew 8x in 2024 alone (GitClear 2025, 2026). Two peer-reviewed papers confirm agents "frequently disregard code reuse opportunities" (arXiv 2601.21276, 2504.12608). *(this repo: three FHIR mappers, three hashers, two atomic writers, five date parsers.)*
2. **Each session starts with no memory.** Nothing persists between sessions by default, so decisions get re-litigated and conventions resurface in a new shape. The measurable footprint is problem 1.
3. **It is more complex than the human version of the same thing.** Matched human-vs-GPT-4 solutions: LLM code had significantly higher cyclomatic complexity, from "comprehensive" validation and error handling nobody asked for (arXiv 2501.16857). *(this repo: QA is 1143 lines in 5 files where Tebra's check is 82 flat lines.)*
4. **It defends against things that never happen.** Over-abstraction and defensive branches accumulate a little per turn. The bug taxonomy for LLM code includes "prompt-biased code" and "missing corner case" side by side: it guards the imagined case and misses the real one (arXiv 2403.08937). *(this repo: ~60% of the PF-source tests exercise inputs no vendor has produced.)*
5. **Tests that look like tests.** Reviewers caught incorrect LLM-written assertions only 49% of the time, worse than a coin flip, with equal confidence either way (arXiv 2607.08885). Frontier models rewrite tests and hardcode outputs when that is easier than the task (METR, Cursor). *(this repo: #411 was "found" by a helper that bypassed the command layer; the fault was the probe.)*
6. **Prose that narrates instead of stating.** AI over-generates comments relative to humans (arXiv 2408.14007). Whether the comments are *bad* is contested; that they crowd out the code is not. *(this repo: 38% of source lines are prose; 56 docstrings narrate bug history that git already holds.)*
7. **A wrong context file makes it worse.** Human-written CLAUDE.md files raised task success 4%; LLM-generated ones lowered it and cost 20% more. Context files help only when they add what the repo's own docs do not (ETH Zurich, arXiv 2602.11988).
8. **Experienced people get slower, not faster.** In a randomised trial, 16 experienced maintainers took 19% longer with AI on codebases they knew, while believing they were 20% faster (METR, arXiv 2507.09089). The time went to checking near-misses.
9. **Nothing ever shrinks by default.** The default AI workflow is structurally net-positive in lines: refactor share 4%, copy-paste 16% (GitClear). *(this repo: the one "de-bloat" commit netted −16 lines; every refactor since added.)*
10. **Two subsystems are big because the problem is.** OCR-to-layout inference (docling's own module: 2210 lines) and C-CDA parsing (medplum's converter: 11,342 lines, twice this repo's) resist compression. Everything else measured 3–14x larger than the best small reference (R6, cloned and counted).

## The rules

1. **Search before you write.** Name what you searched for and what you found. A second implementation of an existing thing is a defect, not a style choice. No canonical source states this as a checkable procedure; this repo writes it as one (R4 gap; #412, #413 as the worked example).
2. **One way per problem.** Isomorphic problem, isomorphic solution. Same name in two packages is a smell. A guard test fails when a second recipe appears (#413's guard is the pattern).
3. **DRY is for knowledge; copying is for mechanism.** A clinical mapping or business rule lives in exactly one place. Five self-contained lines with no shared meaning may be copied. When unsure which it is, duplicate: a wrong abstraction costs more to unwind than a duplicate (Hunt & Thomas; Pike; Sandi Metz).
4. **Never abstract from one example.** Write the concrete version twice; extract on the third with the same semantics (Muratori). An abstraction with one caller is inlined.
5. **A branch needs a receipt.** A defensive branch, a config flag, or a test exists for a filed `#NNN`, a real vendor sample, or a spec clause. Speculation is cut (Fowler/Beck YAGNI; Ousterhout: every special case is a permanent tax).
6. **Define the error out of existence.** When a caller could misuse an interface, reshape the interface so the misuse cannot occur, rather than adding a check for it (Ousterhout).
7. **A comment says what the code cannot.** Why, a hidden constraint, a surprise. Never what the line below does; never the task, issue, or history: git holds that and a comment about it rots (Ousterhout ch. 13; Kernighan & Pike; cursorrules 12–13).
8. **Rewrite unclear code instead of explaining it** (Kernighan & Plauger). Clear beats clever, every time (Go proverbs).
9. **Read the whole neighbourhood first.** Understand before touching: what it does, what calls it, what it calls, why it might be this shape. If you cannot answer, you are not ready to edit (Fowler; Chesterton's fence via addyosmani, grug).
10. **Delete, do not disable.** Commented-out or flagged-off code is noise that costs readers certainty (Batchelder). Code is a liability, not an asset (Feathers).
11. **Golden master first, then cut.** Characterisation tests capture what the software does now, not what it should; a deterministic-time seam makes outputs comparable; scrub narrowly and only what the subject generates (Feathers; ApprovalTests; SOURCE_DATE_EPOCH).
12. **Dead means three tools agree.** Coverage-zero ∩ static-unused ∩ unreachable-in-the-import-graph. grep is not code intelligence. Dynamic dispatch and plugin registration are what deletion breaks first (vulture README; goal-sloc).
13. **Small, single-concern PRs.** Defect detection collapses above roughly 400 changed lines (Cisco/SmartBear; Rigby & Bird). Refactors ship separately from features. Above 500 lines of mechanical change, write a script (addyosmani "rule of 500").
14. **Classify every reduction.** Structural (dead code, duplication, relocation, re-architecture) versus cheap (comment trimming, formatting). If cheap levers dominate, you are gaming yourself; stop and find structural work or report the well dry. Measure code lines and prose lines separately (goal-sloc self-audit).
15. **Never change the ruler.** Excluding paths, widening the formatter, deleting a feature: gaming. Re-baselining a gate to pass: gaming. The count of `#NNN` regression guards may not fall (goal-sloc gaming table; this repo's never-rules).
16. **Track done by shape, not lines.** Complexity trend, green suite, byte-identical corpus pin and snapshot. Lines are the proxy; behaviour is the goal (SonarSource; goal-sloc).
17. **Hooks for zero-exception rules, prose for judgement.** Anything that must happen every time is a gate with an exit code; the rule file explains the why (Anthropic best practices).
18. **The rule file is short and non-derivable.** Under 100 lines ideal. Only what cannot be inferred from reading the code. "Write clean code" is not a rule (ETH Zurich; Anthropic; agents.md's own 43-line file).
19. **Verified means touched.** A run claim carries its exit code; a file claim its bytes; a UI claim the rendered control. "Should work" is not a sentence in a report (cursorrules 17; thomas-to-jesus).
20. **Stop when the delta is churn.** A slice under 200 lines, a diff you cannot explain in one sentence, a pin that moved, a test you want to skip: stop, report, do not manufacture progress (goal-sloc §3).

## Where this repo differs from the reference

Anastomosis is deliberately broader than Tebra: more sources, more destinations, a GUI, learn-from-sample. Breadth is not the disease. The disease is that every capability was built three times with a different variable, guarded against inputs nobody has seen, and explained at length in the file rather than once in a rulebook. The cure keeps every capability and removes the second and third copies, the imagined guards, and the essays. The two subsystems with an honest size (C-CDA parsing, layout inference) get a descriptor table and a prose sweep, not a rewrite.
