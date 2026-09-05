---
name: veteran-programmer
description: >
  The posture a twenty-year maintainer brings to a file, written down so an
  agent can take it: read the whole neighbourhood before editing, find what
  already does this before writing it, prefer the flat and boring shape, and
  ask what could be deleted before asking what to add. Load at the start of
  every session that will touch src/ or tests/, alongside model-hierarchy.
  The other skills in this set (one-way, comments-that-earn-their-lines,
  no-edge-case-without-a-sample, first-principles-audit,
  banach-tarski-refactor) are this posture applied to one question each.
---

# Veteran programmer — what the experienced hand does that the model does not

The measured gap is not skill; it is habit. Across 623M code changes the
share of copy-pasted lines doubled and the share of refactored lines fell
from a quarter to under four percent (GitClear 2026). Agents "frequently
disregard code reuse opportunities" (arXiv 2601.21276). Matched solutions
from a model carry more branches than a human's, from validation nobody
asked for (arXiv 2501.16857). Each of those is a habit a veteran has and
the default session lacks. This skill is the habit.

## Before touching anything

- [ ] **Read the neighbourhood.** The file, its callers, what it calls, and
      the test that pins it. If you cannot say in one breath what the file
      is for and who depends on it, you are not ready to edit it (Fowler:
      read, gain insight, put the insight back).
- [ ] **Search before you write.** Before a new function, class, helper or
      constant: `rg` for the concept by three names (what it does, what it
      returns, what the neighbours call it). Record what you searched and
      what you found in the commit or PR body. A second implementation of
      something that exists is a defect, not a style choice (#412, #413:
      four hand-written copies of one HL7 II rule cost five issues).
- [ ] **Read the rulebook.** `docs/RULES.md` is the settled word. If the
      rule you need is not there, decide, and add it in the same PR.
- [ ] **Find the Tebra equivalent.** For anything the reference script
      also does, read how it did it first. It is usually flatter.

## While writing

- **Flat over clever.** Clear is better than clever (Go proverbs). If you
  are clever enough to write it you may not be clever enough to debug it
  (Kernighan). A function a colleague can read top to bottom without
  scrolling is the target; the complexity ratchet in `tools/check.sh` is
  the floor, not the goal.
- **Boring over novel.** The stdlib, then the dependency already in
  `pyproject.toml`, then a new dependency, then hand-rolled. `zipfile`,
  `shutil`, `json`, `hashlib`, `os.replace` do most of what this tool does.
- **One way.** Same problem, same shape, same name. A helper in two
  packages under two names is a bug in waiting (see `one-way`).
- **Define the error out of existence.** When a caller could misuse an
  interface, reshape the interface so it cannot, rather than adding a
  check (Ousterhout). A check that guards an input nobody produces is a
  permanent tax (see `no-edge-case-without-a-sample`).
- **Copy mechanism, share knowledge.** Five self-contained lines with no
  shared meaning may be copied. A clinical mapping, a code system, an
  identity rule lives in exactly one place. Unsure which it is? Copy: a
  wrong abstraction costs more to unwind than a duplicate (Metz). Extract
  on the third occurrence with the same semantics, never the first
  (Muratori).
- **Inline the one-caller abstraction.** A wrapper with one caller, a
  class with one method, a factory for one product: inline it. Carmack
  from below, Ousterhout from above; both reject the shallow middle.
- **Name it for what it is.** `patients`, not `data`; `chart_path`, not
  `p`. The name carries what a comment would otherwise have to.
- **Write the comment only when the code cannot say it** (see
  `comments-that-earn-their-lines`).

## Before calling it done

- [ ] **Ask what to delete.** Every PR that adds lines names what it
      removed, or says why nothing could go. Code is a liability (Feathers);
      disabled code is noise (Batchelder). Delete, never comment out.
- [ ] **Explain it in one breath.** If the change needs a paragraph to
      justify, it is probably two changes or the wrong one.
- [ ] **Touch it.** Run the real command, open the real file, read the
      exit code unpiped (thomas-to-jesus). "Should work" is not a sentence.
- [ ] **Keep it small.** Defect detection collapses above roughly 400
      changed lines (Cisco/SmartBear). Refactor and feature ship apart.

## The questions, for the wall

What does this do · why does it exist · who depends on it · did something
already do this · what would a veteran delete here · could a colleague
read it in one breath · did I run it.
