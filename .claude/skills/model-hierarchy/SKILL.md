---
name: model-hierarchy
description: >
  Time-indifferent model routing policy: which tier of model does which kind
  of work, regardless of current model names. Use at the START of any
  substantive session and whenever deciding whether to do work directly or
  delegate to a subagent. Applies globally, not just to this repo.
---

# Model hierarchy — route work by ROLE, not by model name

Model names rot; roles don't. The rule is the four-tier role system below.
Re-derive the name mapping from the provider's **current** lineup whenever
models change — the newest frontier model always takes the Orchestrator seat
and everything shifts accordingly.

## The four roles

| Role | Definition (time-indifferent) | Does | Never does |
| --- | --- | --- | --- |
| **ORCHESTRATOR** | The newest frontier/SOTA reasoning model, largest context available | All main thinking: planning, task decomposition, architectural decisions, spec writing, reviewing every subagent's output, final synthesis, skills/plans/commit messages of record | Web research; mechanical edits; anything a cheaper tier does acceptably |
| **BUILDER** | One tier below frontier, large context, strong coding | Writing production code to orchestrator specs; substantial refactors; adversarial QA review (probing needs strength); mid-level management of grunt subtasks | Architectural decisions; accepting its own work without orchestrator review |
| **SCOUT** | The current best cheap, fast, web-capable model | All online research, doc fetching, fact verification, ecosystem surveys | Writing production code; making decisions |
| **GRUNT** | The cheapest competent model | Mechanical chores from exact specs: renames, sweeps, data-file generation, log triage | Anything requiring judgment |

**Current mapping (verify against provider lineup; last updated 2026-06):**
Orchestrator = Claude Fable 5 [1m] · Builder = Claude Opus 4.8 [1m] ·
Scout = Claude Sonnet 4.6 · Grunt = Claude Haiku 4.5.

## Routing rules

1. **Main thinking is reserved.** Only the Orchestrator plans, decides, and
   synthesizes. If the Orchestrator catches itself doing scout or grunt work
   that delegation would handle, it delegates.
2. **Every delegation carries**: the exact task, the repo invariants that
   apply, acceptance criteria, the verification command to run, and an output
   format with a line budget. A subagent without acceptance criteria is a
   token bonfire.
3. **Nothing merges unreviewed.** Builder output goes through the
   quality-gate skill (which includes a Builder-tier adversarial QA review)
   and then Orchestrator judgment before commit.
4. **Escalate, don't grind.** If a tier fails the same task twice, escalate
   one tier up rather than retrying a third time. Note the escalation.
5. **Parallelize independent work** across subagents in one message;
   sequence only true dependencies.
6. **Honest accounting.** Subagents report actual gate/verification output
   verbatim. The Orchestrator restates outcomes from evidence, never from a
   subagent's summary alone.
7. **Research is never embedded in code paths.** Scout findings come back
   marked VERIFIED (URL) / UNCERTAIN, and only VERIFIED facts enter code,
   fixtures, or capability registries (the no-hallucination rule).

## Project agent bindings

This repo pins the roles in `.claude/agents/`:
`researcher` (Scout) · `implementer` (Builder) · `qa-reviewer` (Builder,
adversarial) · `grunt` (Grunt). Use them via the Agent tool; the Orchestrator
is whatever model is reading this.
