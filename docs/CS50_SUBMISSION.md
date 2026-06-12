# CS50x final-project submission checklist

> **Caveat — read first.** CS50x's requirements and tooling change between
> editions. This checklist is written from the author's understanding of the
> CS50x final-project requirements as documented at
> <https://cs50.harvard.edu/x/project/>. **Verify every item against that live
> page before you submit** — it is the single source of truth, and this file is
> not fetched or auto-updated. Where this doc and the live page disagree, the
> live page wins.

## What CS50x's final project requires

Per <https://cs50.harvard.edu/x/project/> (confirm against the live page):

- **A project of your own design**, more complex than the course's problem
  sets — which Anastomosis comfortably is.
- **A `README.md`** in the project's root that documents the project. CS50x
  typically asks the README to explain *what* the project is, *what's in each
  file* you wrote, and *the design decisions* you made.
- **A demo video, no more than 3 minutes long**, in which you present your
  project to the world — uploaded to YouTube (or similar) and linked from the
  submission.
- **Honesty / academic-honesty compliance** — the work is your own; outside
  help and tools are disclosed. (This project's AI-assisted authorship is
  disclosed in `paper/paper.md` under "AI usage disclosure"; mirror that
  disclosure wherever CS50 asks.)

## How this repository already satisfies the README requirement

`README.md` in the repo root already contains:

- a "How it works — file-by-file" section (the per-file walkthrough),
- a "Design rationale" section (the design decisions and trade-offs),
- a `**Demo video:**` line that is the **one remaining TODO** — paste the
  video URL there once it's uploaded.

So the required project README is effectively complete except for the video
URL. (CS50 may want a README scoped to the submitted slug rather than the whole
multi-milestone repo — if so, the same three sections transplant directly.)

## The submit50 slug convention

CS50x final projects are submitted with `submit50` using the final-project
slug. As of this writing the slug is:

```
submit50 cs50/problems/2025/x/project
```

The year segment (`2025`) tracks the course edition and **changes every year** —
the current correct slug is shown on the final-project page itself. Confirm it
there before running `submit50`; do not assume the segment above is current.

`check50` is generally **not** run for the final project (there's no
auto-grader for an open-ended project); `submit50` performs the submission.
Re-confirm whether your edition expects `check50` on the live page.

## Your steps (the human's act — nothing in this repo submits)

Submission is **your** action. This repository contains no code that uploads,
submits, or contacts CS50 on your behalf — by design.

1. Record the demo video following [`docs/DEMO_STORYBOARD.md`](DEMO_STORYBOARD.md),
   keeping it **≤ 3 minutes**, on synthetic data only.
2. Upload it (e.g. to YouTube) and copy the public URL.
3. Paste the URL into `README.md` at the `**Demo video:**` line (and into the
   submission form / `paper/paper.md` if you also submit the JOSS paper).
4. Re-read <https://cs50.harvard.edu/x/project/> and confirm the current
   requirements, the README expectations, and the exact `submit50` slug.
5. Run `submit50 <the-current-slug>` from the project directory and complete
   any web-based submission form CS50 links you to.
6. Confirm the submission appeared in your CS50 gradebook / submissions view.

## Local sanity check before you submit

Not a CS50 requirement, but run the project's own gate so the demo commands
won't surprise you on camera:

```bash
bash tools/check.sh        # ruff + mypy --strict + pytest + PHI scan
```

Expect it to print `ALL GATES GREEN`.
