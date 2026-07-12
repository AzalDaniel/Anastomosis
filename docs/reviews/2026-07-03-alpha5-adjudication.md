# 0.5.0-alpha — adjudication of the external alpha-4 review

Date: 2026-07-03. Reviewed artifact: `main` @ `6b56c27` (= the 0.4.0 merge),
verified by the external reviewer (Codex) with its own gate run (ruff, format,
mypy --strict, product smoke, full pytest from the source ZIP). Verdict given:
"REQUEST CHANGES before beta/CS50 submission, but not because alpha 4 is
structurally broken." Every decision below was re-derived and re-verified
against the code before any correction was implemented.

## Finding-by-finding

### F1 — Source identifiers in logs breach the SECURITY.md contract — ACCEPTED
This finding correctly critiques a fix made in 0.4.0 itself: the archive
deliverer's missing-PDF warning was changed to log `record.patient.id` and the
adjacent comment calls it "the opaque patient id" — but `Patient.id` is the
source system's own identifier (`PatientPracticeGuid` in the PF adapter,
`PERSON_ID` in the Oracle adapter), and on the machine where the export lives
it is trivially linkable back to a patient. Under the contract's own words
("counts, field names, and opaque ids"), a source GUID is an identifier, not
opaque telemetry. The same applies to encounter/event ids and to `item_key`
(which embeds an encounter GUID). Fix adopted: `safe_log_id()` in
`core/logutil.py` — an HMAC-SHA256 surrogate keyed by a **per-process
ephemeral key**, so log lines about the same record correlate within a run
(the operational need) but are unlinkable across runs and cannot be confirmed
against the export by anyone without that run's key (a fixed baked-in key
would permit confirmation attacks; the reviewer's sketch used an app-level
key, and the ephemeral variant is adopted as strictly stronger). Applied to
every stdlib-logging site that interpolates a source-derived id. Scope
boundary, deliberately: the CLI/GUI *display* surfaces (which encounter
failed to render, the upload console's item keys) and the resumability
ledger keep real ids — those are operator-facing working surfaces inside
hardened directories, not logs; SECURITY.md governs logs and now says so
precisely.

### F2 — `phi_scan` assumes a git checkout — ACCEPTED, fallback over skip
`tools/phi_scan.py` enumerates via `git ls-files --cached --others
--exclude-standard`; from a source ZIP/sdist there is no `.git` and
`test_whole_repo_is_clean` fails (the reviewer proved a `git init` makes it
pass). Of the two fixes offered, the fallback walk is adopted over
skip-with-a-message: a PHI scanner that silently stops scanning for exactly
the users who obtained the code outside git is a scanner with a hole in it.
The fallback is a deterministic recursive walk with an explicit prune set
(VCS/cache/venv/build directories) used only when git enumeration is
unavailable; under git, behavior is byte-identical.

### F3 — CS50 video placeholder — ACKNOWLEDGED, not actionable here
Only the author can record the ≤3-minute demo. The placeholder stays until
submission time; everything else in the CS50 packet (README sections,
DESIGN.md, per-file AI citations) already exists.

### Refactor suggestions (split PF mapper by table family, C-CDA builder by
section) — DEFERRED AGAIN, on the reviewer's own reasoning
The review itself says "no radical deletion" and "refactor incrementally";
both files are domain mappers already decomposed into focused per-resource
functions, with outputs pinned byte-identical by goldens. Splitting them is
churn without a defect, so it stays on the M6 backlog. The review's
performance judgment (no remaining algorithmic sink; rendering dominates;
profile before parallelizing) is accepted as-is — no action.

### Scores — noted
Security/privacy 8.0 "until identifier logging is fixed" is the release
driver; F1 is therefore alpha 5's headline change.

## Alpha-5 scope beyond the review (owner request)

**A downloadable installer, end to end.** The installer itself has existed
since 0.3.0 (Nuitka standalone GUI + CLI, Inno Setup, WebView2 bootstrap,
CI-smoke-tested by silent install + installed self-check) and the
launch-on-finish checkbox was already present — but **no release carrying it
was ever published**, because publishing required a `v*` tag push and the
0.4.0 tag push was blocked by environment credentials. Alpha 5 closes that
structurally: the release path gains a `workflow_dispatch` publish mode
(guarded to `main`, version-asserted) in which the release action creates
the tag itself — so a release can be shipped from the Actions tab with no
terminal access at all. Installer polish added: desktop-icon optional task,
`UninstallDisplayIcon`, and the AGPL license page. Deliberately NOT changed:
the per-machine install scope (the PATH machinery is built for HKLM and the
whole flow is CI-proven — switching to per-user would trade tested behavior
for a UAC-free first run), and the `AppId` (changing it would orphan
existing installs). Code signing and a proper application icon remain the
two known gaps between "works like a native app" and "indistinguishable from
one"; both are GA items (signing needs a purchased certificate; SmartScreen
guidance is already in the README).
