# Security Policy

## Reporting a vulnerability

Email **arabicphysicist@gmail.com** with the subject line `Anastomosis security: <one-line summary>`.

Please include: the affected version/commit, a minimal reproduction, the
impact you observed, and your preferred contact for follow-up. If the
issue involves PHI exposure, **do not** include real patient data in the
report — synthetic reproductions only. PGP available on request.

Expect an initial acknowledgement within 72 hours and a disclosure
timeline within 14 days. Coordinated disclosure is the default.

## Threat model

Anastomosis reconstructs, verifies, and re-homes clinical records on
behalf of practices. The runtime touches third-party clinical data
(EHI exports, C-CDA documents, FHIR bundles, browser-automated EHR
sessions) and produces PDFs, JSON, and archives that contain protected
health information (PHI). The repository itself is treated as
**untrusted to contain real PHI** — only synthetic data may enter it.

Anastomosis processes PHI **locally**. The core pipeline
(ingest → reconstruct → QA → archive) makes no network calls; nothing is
transmitted, telemetered, or phoned home. The assets to protect are
therefore local: the source export, the reconstructed documents, the
upload tracking database, and any credentials the *operator's own browser
session* holds. The threat surface is the local machine, the output
directories, and any pack (plugin) code the operator chooses to run.

In scope for this policy:

- Code paths that ingest, transform, render, deliver, or persist
  patient data (`sources/`, `core/`, `reconstruct/`, `qa/`, `deliver/`).
- Browser-automation drivers and CDP attach paths (`deliver/browser/`).
- The PHI scanner and its hash-list discipline (`tools/phi_scan.py`,
  `tools/phi_allowlist.txt`).
- Default file permissions, output-directory hygiene, log redaction,
  and dependency hygiene.

Out of scope:

- Vulnerabilities in upstream dependencies (please report to those
  projects; we'll track and pin once a fix lands).
- Behavior of destination EHRs and their APIs.
- Issues that require physical access to a logged-in user's machine,
  or that depend on the user pasting attacker-supplied credentials.

## Operator responsibilities

The tool hardens what it creates; keeping it safe afterwards is yours:

- Run on an access-controlled machine with full-disk encryption. The
  tool's directory hardening resists siblings and casual access, not a
  compromised host.
- Treat every output directory (archives, rendered PDFs, `tracking.db`)
  as PHI at rest. The tool creates them with restrictive permissions and
  drops a warning README inside; retention and disposal are your call.
- **Never paste real patient data into GitHub issues.** Reproduce bugs
  with the synthetic fixtures under `tests/fixtures/`.
- You (or your practice) are the HIPAA covered entity or business
  associate. The Anastomosis authors are not a business associate and no
  BAA exists.
- Review third-party pack code before running it against real data. Packs
  are executable code; built-in packs are reviewed here, anything else is
  yours to vet.

## Posture and controls

The repository ships several defensive controls that are part of the
contract — regressions in any of them are security findings:

- **No network in the core pipeline.** Ingest, reconstruct, QA, and
  archive make no network calls; the unit suite runs behind a
  loopback-only network guard (`pytest-socket`), so an outbound call from
  the core path fails the tests loudly. The delivery paths that *do* talk
  to an EHR (FHIR API push, browser automation) say so explicitly and only
  reach the destination the operator configures.
- **Credentials are never stored by the tool.** Browser delivery attaches
  over CDP to a Chrome session the operator launched and logged into
  (loopback only); API delivery reads credentials from the environment or
  config and never writes them back.
- **No-real-PHI rule.** The PHI scanner (`tools/phi_scan.py`) runs in
  pre-commit and CI on the full tree, using a hashed deny-list plus
  generic shape patterns (SSN, non-fixture GUIDs, non-555 phones,
  DOB-adjacent dates). Synthetic-data conventions are documented in
  `docs/PLAN.md` and `tests/fixtures/*/README.md`.
- **Log redaction.** `core/logutil.py` provides a `RedactionFilter`
  (SSN/phone/email/date shapes, including the `MM-DD-YYYY` filename form),
  `exc_tag()` so input-derived exceptions never log their message — only
  the exception type — and `safe_log_id()`, a run-scoped HMAC surrogate
  for source-derived identifiers (patient/encounter/event GUIDs, upload
  item keys): log lines about the same record correlate within a run, but
  the surrogates are keyed by a per-process ephemeral key, so they are
  unlinkable across runs and cannot be confirmed against the source
  export. The redacting handler is installed at both application entry
  points (the CLI root callback and the GUI main), idempotently. The
  convention is "log counts, field names, and `safe_log_id` surrogates —
  never values, never raw source identifiers, and never a path under an
  output directory." (Operator-facing display surfaces — the CLI's
  which-encounter-failed lines, the upload console, the resumability
  ledger inside the hardened output directory — are working surfaces,
  not logs, and deliberately keep real ids.)
- **Output hygiene.** `core/output.py` creates output directories
  `0o700` (owner-only) on POSIX; on Windows NTFS it strips ACL
  inheritance and grants access only to the current user, SYSTEM, and
  Administrators (the same posture as CPython's `os.mkdir(mode=0o700)`
  and Win32-OpenSSH), re-applied on every run. On filesystems without
  ACLs (FAT32/exFAT) it warns loudly instead. A PHI-warning README
  lands in every output root on every platform.
- **Loud failures.** Unknown source formats raise; sentinel dates
  (`1/1/0001`) and explicit null tokens (`\N`) return `None`; nothing
  vanishes silently. This is enforced in `core/timeutil.py`,
  `core/textutil.py`, and the source adapters.
- **Lossless guarantee.** Every source field that the adapter does not
  consume rides a namespaced `extensions` dict on the canonical model
  and survives FHIR/C-CDA round-trip. Mapping tables explicitly declare
  consumed columns so additions are loud, not silent.
- **Pack trust model.** Built-in template packs are implicitly trusted;
  third-party packs from `--pack-dir` or entry points execute code and
  therefore require explicit opt-in (`allow_external=True`). Pack
  signing and hash-pinning are tracked in the M6 security backlog
  (`docs/PLAN.md`).
- **Strict gates.** Every commit passes `ruff check` (incl. bandit-S
  and naive-datetime rules), `ruff format --check`, `mypy --strict`,
  `pytest`, and the full-tree PHI scan via `tools/check.sh`. The gate
  runs unmasked (pipefail; never piped through `tail`).
- **Adversarial review.** Substantive changes pass a codified review
  pipeline before merge: a PHI/losslessness compliance pass that halts on
  any finding, then a general quality pass, then a retro-compatibility
  pass against every existing caller of the surface being changed.
  Reviewers report findings and hold no approval authority. This has
  already caught real blockers (substring matching that false-PASSed
  missing vitals; FHIR placeholder strings that corrupted charted values)
  before they merged.
- **CI least privilege.** Workflows declare `permissions: contents: read`
  by default; releases require explicit elevation.
- **CI supply-chain pinning.** Every action any workflow runs — first-party
  `actions/*` included, not just third-party ones — is pinned to a full
  commit SHA, never a movable tag. The jobs that attach a release or publish
  a package (`release` in `windows-package.yml`, `publish` in `release.yml`)
  run under a dedicated GitHub Environment, isolating whatever secrets a
  future signing step needs from every build/test job. `.github/dependabot.yml`
  cools version updates down for a week before they can reach us, on top of
  GitHub's own default.

## Code scanning & suppression policy (auditable)

An advanced CodeQL workflow is committed at
[`.github/workflows/codeql.yml`](.github/workflows/codeql.yml): the
`security-extended` suite on every push and pull request plus a weekly
schedule. (One-time repository setting: GitHub rejects advanced-setup SARIF
uploads while code-scanning *default setup* is enabled, so default setup
must be disabled in Settings → Code security for this workflow's results to
land.)

Inline suppression takes **three** things, and this policy has now twice
described fewer. Each missing one was found by watching the control fail, not
by reading about it, and the sequence is worth keeping because it is the same
mistake the product itself must never make.

1. **The suppression query has to run.** `security-extended` ships
   `AlertSuppression.ql` but does not run it; the CodeQL CLI computes the
   SARIF's `suppressions[]` property only when that query is requested as a
   pack. That is the `packs:` line on the workflow's init step. Without it the
   SARIF carries no suppressions at all.
2. **Code scanning ignores the property even when it is there.** A correctly
   formed comment, correctly placed, changes nothing on its own.
3. **`advanced-security/dismiss-alerts` reads the property back** and dismisses
   the matching alerts through the API — and only after accepted code is pushed
   to `main`. It never runs for a pull request, feature-branch push, or
   scheduled scan: alert state is repository state, so code that has not been
   accepted cannot mutate it with `security-events: write`.

The history, since a policy that quietly acquires a correct sentence teaches
nobody: six suppressions sat inert in `src/` until a seventh, correctly formed
and correctly placed, failed to clear its alert in #310. The dismissal step was
added — and the alert still did not clear. On the merge of #310 that step ran,
reported success, indexed nine alerts and dismissed none, because step 1 was
missing and the SARIF it read was empty of suppressions. A green step that does
nothing is worse than an absent one, and it survived exactly as long as it took
to read its log instead of its status.

All three together are what make every suppression below an auditable control
rather than a convention.

One consequence is worth stating here rather than leaving to be discovered.
Because dismissal happens only after a merge, a pull request that *introduces*
a suppressed line still fails its own code-scanning check: the alert is new in
that pull request, and nothing has yet accepted the code that suppresses it.
It clears on `main` once the push lands. So a red code-scanning check on a pull
request that adds a suppression is expected, and the reviewer's job is to judge
whether the suppression is justified — not to wait for a green that cannot
arrive before merge. That is a deliberate trade, and the alternative is worse:
letting unmerged code dismiss alerts would destroy the audit trail this policy
exists to keep.


Anastomosis's product surface is writing patient records to disk under the
operator's control, which the `py/clear-text-storage-sensitive-data` rule
cannot distinguish from a defect. Rather than exclude any rule repo-wide,
suppression is **inline and per-site**: each site carries a
`# codeql[rule-id]` comment immediately beside a rationale stating the
guarantee that justifies it. There are exactly two rationales a site may
claim, and they are not interchangeable:

- `PHI-BY-DESIGN` — the site really does write a patient's record, where the
  operator asked for it, under a guarantee that makes that safe (a
  `secure_output_dir`-hardened directory, or field-name-not-value logging).
  The rule is reading the product as a defect.
- `PHI-FREE-BY-CONSTRUCTION` — nothing sensitive reaches the sink at all, and
  the alert is a false positive the code cannot phrase its way out of. This
  claim is the easier one to make and the easier one to be wrong about, so it
  is only accepted alongside a test that fails if it ever stops being true.

The audited suppression sites are exactly:

- `src/anastomosis/deliver/_shared.py` (the per-patient FHIR bundle, written
  the same way by every file-writing deliverer) —
  `py/clear-text-storage-sensitive-data`
- `src/anastomosis/deliver/archive/archive.py` (archive index) —
  `py/clear-text-storage-sensitive-data`
- `src/anastomosis/deliver/bundle/bundle.py` (bundle README) —
  `py/clear-text-storage-sensitive-data`
- `src/anastomosis/deliver/browser/persist.py` (upload manifest) —
  `py/clear-text-storage-sensitive-data`
- `src/anastomosis/deliver/render_index.py` (render-index sidecar) —
  `py/clear-text-storage-sensitive-data`
- `src/anastomosis/deliver/fhir_api/destination.py` (a log line carrying
  the *name* of the matched field, never a value) —
  `py/clear-text-logging-sensitive-data`
- `docs/audits/learned-source/tools/probe_ccda_corpus.py` (the corpus probe
  prints integer counts under keys that are string literals declared in the
  file; all it takes from a chart is a yes/no and a length) —
  `py/clear-text-logging-sensitive-data`, `PHI-FREE-BY-CONSTRUCTION`, proven
  by `tests/unit/test_corpus_probe_emits_no_values.py`, which parses a chart
  and requires that none of its strings appear in the printed output. The
  suppression is a backstop rather than the fix: CodeQL never told us which
  flow it objected to, so the probe was restructured until no flow existed.

A policy test pins this list: every inline suppression under `src/` and
`docs/` must sit beside one of the two rationales and appear in the list
above, so a new suppression cannot land without amending this policy. The
audit tools under `docs/` are in scope precisely because they are run by
hand against real exports — an unwatched corner is where a silenced alert
would actually hide. Every module not listed here remains fully covered by
both rules; if the field-name convention drifts or a writer lands outside a
hardened directory, CodeQL alerts again.

Storage-at-rest encryption remains a separate, opt-in operator concern
(BitLocker / FileVault / dm-crypt); directory hardening resists siblings
and casual access, not a compromised host.

## Synthetic-data conventions (for contributors)

Fixtures must use only:

- GUIDs prefixed `feedface-` or `00000000-`.
- Phone numbers in the **555-01xx** reserved exchange range
  (per US NANP convention for fictional numbers).
- SSN area numbers `000`, `666`, or `>= 900` (never-issued ranges).
- `example.com` email addresses.
- Fictional names that are obviously not real people.

Any commit attempting to introduce data outside these conventions is
blocked by the scanner. False positives are added to
`tools/phi_allowlist.txt` with a written justification, never by
relaxing the scanner.

## Hall of thanks

To be populated after the first coordinated disclosure. If you'd like to
remain anonymous, say so in your report.
