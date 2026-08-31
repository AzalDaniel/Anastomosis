---
name: thomas-to-jesus
description: >
  The doubting-Thomas doctrine (John 20:24-29): nothing is reported as
  working that has not been personally touched — the real command run, the
  real file's bytes read, the real page driven — and the touch itself is the
  report. Governs every claim in a completion message, PR body, review
  verdict, or issue comment. Composes with potemkin-check (which says WHAT
  to check; this says what counts as having checked it).
---

# Thomas to Jesus — believe the wound, not the word

Thomas would not believe the resurrection on testimony; he required his
finger in the wounds, and got it. The order matters both ways: the doubt was
legitimate, and the evidence was granted rather than resented. In this repo
the same bargain is law. An agent's account of its own work is testimony;
the artifact is the wound. Only the wound settles it.

This is not ceremony. The gap between inference-from-code and
observation-of-behavior is where every serious failure here has lived: the
2,103 documents that all "parsed" while eleven collections came back empty;
the gate exit code masked by a `| tail`; the wording that called credited
authors "dropped" and read plausibly until a probe held two documents side
by side. Each was inference sounding like observation.

## The rules

- [ ] **No inference-only claims.** "The code should do X" is not a
      permitted sentence in a report. Rewrite every claim as an action plus
      an observation: "ran X, observed Y". If the action was not run, the
      claim is not made.
- [ ] **The touch requirement.**
      - A file claim quotes the actual bytes on disk (read it back, hash
        it), not the diff that was written.
      - A run claim pastes the actual stdout/stderr and the actual exit
        code — captured so a pipe cannot launder a failure into silence.
      - A UI claim drives the real control in the real page and captures
        what rendered; a GUI test that never opened the shipped page has
        touched a mock, not the product.
      - A "merged/closed/green" claim re-reads the live state, not the
        memory of having acted.
- [ ] **The evidence ledger.** Every reported result carries its provenance
      inline: claim → command → exact output or hash. A claim without a
      ledger line is testimony and is marked as such.
- [ ] **Second derivation for clinical correctness.** Anything touching
      identity, field mapping, or conservation gets re-derived a second,
      independent way before it is believed once (two documents, two tools,
      or the deliberately-broken control pair).
- [ ] **Self-report is not a source.** A subagent's "gate green" is a claim
      to verify, not a fact to relay: re-run the gate or read the raw log
      before repeating it. The same courtesy runs upward — this agent's own
      earlier statements are testimony too, and stale ones get re-touched
      before they are re-asserted.
- [ ] **Adversarial pass before "done".** Try to break the claim: the
      malformed input, the empty export, the second run into the same
      folder, the concurrent run. Record what was tried, whichever way it
      went.
- [ ] **UNVERIFIED is a verdict.** When the artifact cannot be touched — no
      credentials, no network, an environment only the owner has — the claim
      is stated as UNVERIFIED with the missing touch named. The gap is never
      filled with plausible prose; Thomas's week of doubt was honest, and
      the report that bridges it with confident words is not.

## The one exception there isn't

There is no seniority exemption, no "it's obviously fine" exemption, and no
deadline exemption. "Blessed are those who have not seen and yet believed"
is a grace extended to people — a migration tool holding someone's chart
gets the finger in the wound, every time.
