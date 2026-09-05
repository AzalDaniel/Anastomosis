---
name: model-hierarchy
description: >
  Route work by role, not by model name: an Orchestrator that plans and
  decides, a Builder that writes and reviews adversarially, a Scout that
  researches, a Grunt for mechanical chores. Use at the start of any
  multi-agent task, when choosing what to delegate and to whom, and when
  the provider's lineup changes. Records this repository's current program
  routing (the refactor toward the eighth alpha).
---

# Model hierarchy — route work by ROLE, not by model name

Model names rot; roles don't. Re-derive the name mapping from the
provider's **current** lineup whenever models change: the newest frontier
model takes the Orchestrator seat and everything shifts accordingly.

## The four roles

| Role | Definition | Does | Never does |
| --- | --- | --- | --- |
| **ORCHESTRATOR** | The newest frontier reasoning model, largest context | Planning, decomposition, architecture, spec writing, reviewing every subagent's output, final synthesis, skills/plans/commit messages of record | Web research; mechanical edits; anything a cheaper tier does acceptably |
| **BUILDER** | One tier below frontier, large context, strong coding | Production code to Orchestrator specs; substantial refactors; adversarial QA review; managing grunt subtasks | Architectural decisions; accepting its own work without Orchestrator review |
| **SCOUT** | The best cheap, fast, web-capable model | Online research, doc fetching, fact verification, ecosystem surveys, codebase reconnaissance | Making decisions; writing code without a written spec and a Builder review |
| **GRUNT** | The cheapest competent model | Mechanical chores from exact specs: renames, sweeps, data-file generation, log triage | Anything requiring judgement |

**Current mapping (verify against the provider lineup; last updated
2026-09):** Orchestrator = the Fable tier · Builder = the Opus tier ·
Scout = the Sonnet tier · Grunt = the Haiku tier.

## This program's routing (the refactor toward the eighth alpha)

The owner set it: **Fable orchestrates and holds the design; Opus
architects and reviews adversarially; Sonnet scouts, breadth and depth, in
real time, and writes bulk code.** That last clause widens the Scout role
for this program only, under two conditions that do not bend: Sonnet
writes to a spec Opus or Fable wrote, and nothing Sonnet writes merges
without an Opus review and the Orchestrator's own drive of the artifact.
Research still enters code only as VERIFIED facts with a URL.

## Routing rules

1. **Main thinking is reserved.** Only the Orchestrator plans, decides,
   and synthesises. If it catches itself doing scout or grunt work, it
   delegates.
2. **Every delegation carries** the exact task, the repo invariants that
   apply (`docs/RULES.md` by section), acceptance criteria, the
   verification command, and an output format with a line budget. A
   subagent without acceptance criteria is a token bonfire.
3. **Nothing merges unreviewed.** Builder and Scout output goes through
   `quality-gate` (which includes a Builder-tier adversarial review) and
   then Orchestrator judgement before commit.
4. **Escalate, don't grind.** A tier that fails the same task twice is
   escalated one tier up, not retried a third time. Note the escalation.
5. **Parallelise independent work** in one message; sequence only true
   dependencies.
6. **Honest accounting.** Subagents report actual gate output verbatim.
   The Orchestrator restates outcomes from evidence it touched, never from
   a summary alone (`thomas-to-jesus`).
7. **Research is never embedded in code paths.** Scout findings come back
   VERIFIED (URL) or UNCERTAIN; only VERIFIED enters code, fixtures, rules
   or skills.
8. **Every agent gets its own worktree.** Never the shared checkout, never
   `git stash`, never a mutation in a working branch.

## Project agent bindings

`.claude/agents/` pins the roles: `researcher` (Scout) · `implementer`
(Builder) · `qa-reviewer` (Builder, adversarial) · `grunt` (Grunt). The
Orchestrator is whatever model is reading this.
