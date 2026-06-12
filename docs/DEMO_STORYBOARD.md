# Demo storyboard — Anastomosis (CS50 final project)

The recording script for the ≤3-minute demo video. Eight scenes, timed to
land under the CS50 three-minute norm. The video URL goes in `README.md`
(`**Demo video:**`) and `docs/CS50_SUBMISSION.md` after recording.

**Recording rules (non-negotiable):**

- **Synthetic data only.** Every record on screen is a `feedface-` GUID
  fixture (`tests/fixtures/pf_tebra_v9/`, `tests/fixtures/ccda/`,
  `tests/fixtures/synthea/`). No real PHI, ever — that is the project's first
  invariant and it must hold on camera too.
- **Show nothing the code cannot do.** Every command and screen below is
  verified against the repo. Live upload-driving is *not* shown working,
  because the tebra browser pack ships with placeholder (DISCOVER) selectors
  and the registry declares no viable browser route — so the upload scene is a
  dry-run/blurred walkthrough of the console and the verification log, clearly
  labeled as such. Do not stage a fake successful upload.
- **No emoji, no fabricated metrics.** The only numbers spoken are ones the
  audience can verify (the 12,906 figure is stated as self-reported
  predecessor provenance, exactly as `README.md` and `paper/paper.md` phrase
  it).

Running total in the right-hand column is cumulative; keep it ≤ 3:00.

---

## Scene 1 — The problem (0:00–0:25, ~25s)

**On screen:** title card "Anastomosis — reconstruct, verify, re-home clinical
records," then a plain slide with three bullets.

**Narration:**
> "When a US medical practice leaves its EHR, the law guarantees it gets its
> data back — the 21st Century Cures Act EHI export. But the law never says in
> what *format*. So practices get a pile of raw vendor tables, and the
> clinical notes — the part clinicians actually read — routinely don't survive
> the move. A practice I worked with faced an estimated five months of manual
> re-entry, or a five-figure archive-vendor bill. Anastomosis is the missing
> last mile, free and open source."

**Cumulative: 0:25**

---

## Scene 2 — An EHI export on screen (0:25–0:45, ~20s)

**On screen:** a file browser (or `ls`) showing `tests/fixtures/pf_tebra_v9/`
— the synthetic Practice Fusion/Tebra v9 export: ~29 `*.tsv` files
(`patient-demographics.tsv`, `patient-encounters.tsv`,
`patient-encounter-observations.tsv`, `prescription-transactions.tsv`, …).
Open one TSV to show the raw, joined-table shape.

**Narration:**
> "This is what an export actually looks like: twenty-nine tab-separated
> tables you'd have to join by hand to rebuild a single visit. Everything here
> is synthetic — `feedface` IDs, never-issued SSNs, no real patient anywhere
> in this project."

**Cumulative: 0:45**

---

## Scene 3 — One command (0:45–1:10, ~25s)

**On screen:** a terminal. Type and run:

```bash
anast pipeline run tests/fixtures/pf_tebra_v9 --out ./charts --archive ./archive
```

Let the real output scroll: `Detected source: pf-tebra`, the
`N rendered, 0 skipped, 0 failed` line, and the QA pass/warn/fail counts.

**Narration:**
> "One command. It auto-detects the format, joins those tables into a lossless
> canonical model — no field silently dropped — reconstructs each encounter as
> a chart PDF, verifies every rendered document, and builds a searchable
> offline archive. The QA stage runs by default; a failure would exit nonzero."

**Cumulative: 1:10**

---

## Scene 4 — The archive + a faithful PDF (1:10–1:40, ~30s)

**On screen:** open `./archive/index.html` from a `file://` URL (no server).
Use the search box, click into a patient, open a rendered chart PDF. Note the
SOAP sections and that the page loaded with the network panel showing zero
outbound requests.

**Narration:**
> "The archive opens straight from a `file://` URL — plain folders, HTML,
> PDFs, and a FHIR R4 bundle per patient, designed to stay readable for the
> decades that record-retention law requires, with no database and no network
> calls. This replaces a paid legacy-archive subscription with files you own.
> Here's a reconstructed chart — the SOAP note, rebuilt from those raw tables."

**Cumulative: 1:40**

---

## Scene 5 — The desktop GUI (1:40–2:05, ~25s)

**On screen:** run `anast gui`. The liquid-glass dashboard opens. Run the same
pipeline through it and let the ingest → reconstruct → QA counters tick up
live. Show the section-selection matrix (the toggles built from the pack's
section flags).

**Narration:**
> "The same pipeline drives a desktop GUI — the CLI and the GUI run identical
> code. The dashboard shows live counters as records ingest, charts render,
> and QA verifies. The section matrix lets a non-technical user toggle which
> parts of the note to include."

**Cumulative: 2:05**

---

## Scene 6 — The transit map (2:05–2:25, ~20s)

**On screen:** terminal — run:

```bash
anast destination route epic
anast destination route tebra
```

Show the transit map output (the three route options in preference order with
their evidence dates). Then switch to the GUI migration wizard page showing the
same transit map visually.

**Narration:**
> "When you're migrating *into* a new EHR, the router picks the shortest path:
> a vendor API if one exists, then C-CDA import, then browser automation as a
> last resort. Every capability is backed by a cited source — Epic routes by
> FHIR DocumentReference; Tebra has no write API, so it falls to the browser
> route. No capability is ever asserted without evidence."

**Cumulative: 2:25**

---

## Scene 7 — Dry-run / blurred upload console + verification log (2:25–2:50, ~25s)

**On screen:** the GUI upload console (or `anast destination list`) over a
**crafted synthetic ledger** — grouped state counters across the 15-state
machine, the error-inspector flyout showing exception-TYPE histograms only.
Show the L0–L6 verification log/levels alongside. Blur or label any field that
would resemble a real chart; do **not** show a live upload completing.

**Narration:**
> "Delivery is the dangerous part: putting the right note in the wrong chart
> is worse than not migrating at all. So every upload runs an L0-through-L6
> verification ladder — file integrity, a fuzzy identity match with a
> date-of-birth hard-fail, a live patient-banner readback, and a round-trip of
> the stored bytes. The whole run lives in a crash-resumable ledger. This is a
> dry run on synthetic data — live upload-driving needs operator-discovered
> selectors against a real session, which we never invent."

**Cumulative: 2:50**

---

## Scene 8 — Closing (2:50–3:00, ~10s)

**On screen:** a closing slide: AGPL-3.0 badge, the repo URL, "no PHI, ever,"
and "contributions welcome — see CONTRIBUTING.md."

**Narration:**
> "Anastomosis is AGPL-3.0 — anyone offering it as a service must share their
> changes back. It generalizes a private system that reconstructed 12,906
> encounter documents at a 100% final QA pass rate, with zero wrong-patient
> events — engineered so the codebase itself never touches real patient data.
> It's open; come build the next adapter."

**Cumulative: 3:00 (hard cap — trim Scene 1 or 5 first if over)**

---

## Pre-record checklist

- [ ] `bash tools/check.sh` is green (so the live commands won't surprise you).
- [ ] `pip install -e ".[render,gui]"` and `playwright install chromium` done
      on the recording machine.
- [ ] A throwaway output dir (`./charts`, `./archive`) cleaned between takes.
- [ ] The crafted synthetic ledger for Scene 7 prepared (synthetic ids only).
- [ ] Screen scrubbed of any non-synthetic windows/notifications.
- [ ] Final runtime ≤ 3:00; upload scene unmistakably labeled "dry run."
