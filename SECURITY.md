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

## Posture and controls

The repository ships several defensive controls that are part of the
contract — regressions in any of them are security findings:

- **No-real-PHI rule.** The PHI scanner (`tools/phi_scan.py`) runs in
  pre-commit and CI on the full tree, using a hashed deny-list plus
  generic shape patterns (SSN, non-fixture GUIDs, non-555 phones,
  DOB-adjacent dates). Synthetic-data conventions are documented in
  `docs/PLAN.md` and `tests/fixtures/*/README.md`.
- **Log redaction.** `core/logutil.py` provides a `RedactionFilter`
  (SSN/phone/email/date shapes, including the `MM-DD-YYYY` filename form)
  and `exc_tag()` so input-derived exceptions never log their message —
  only the exception type. The redacting handler is installed at both
  application entry points (the CLI root callback and the GUI main),
  idempotently. The convention is "log counts, field names, and opaque
  ids — never values, and never a path under an output directory."
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
- **Adversarial review.** `.claude/skills/quality-gate/` codifies the
  pre-commit pipeline (PHI/losslessness compliance halts first; reviewers
  carry no approval authority); `.claude/skills/polymerase-review/`
  enforces retro-compatibility against existing callers; the
  `qa-reviewer` agent in `.claude/agents/` runs the adversarial pass.
  These have already caught real blockers (substring matching that
  false-PASSed missing vitals; FHIR placeholder strings that corrupted
  charted values) before they merged.
- **CI least privilege.** Workflows declare `permissions: contents: read`
  by default; releases require explicit elevation.

## Code scanning & suppression policy (auditable)

An advanced CodeQL workflow is committed at
[`.github/workflows/codeql.yml`](.github/workflows/codeql.yml): the
`security-extended` suite on every push and pull request plus a weekly
schedule, with the alert-suppression query pack enabled. (One-time
repository setting: GitHub rejects advanced-setup SARIF uploads while
code-scanning *default setup* is enabled, so default setup must be
disabled in Settings → Code security for this workflow's results to land.)

Anastomosis's product surface is writing patient records to disk under the
operator's control, which the `py/clear-text-storage-sensitive-data` rule
cannot distinguish from a defect. Rather than exclude any rule repo-wide,
suppression is **inline and per-site**: each deliberately PHI-writing call
site carries a `# codeql[rule-id]` suppression comment immediately beside a
`PHI-BY-DESIGN` comment stating the guarantee that justifies it (a
`secure_output_dir`-hardened directory, or field-name-not-value logging).
The audited suppression sites are exactly:

- `src/anastomosis/deliver/archive/archive.py` (per-patient FHIR bundle;
  archive index) — `py/clear-text-storage-sensitive-data`
- `src/anastomosis/deliver/bundle/bundle.py` (per-patient FHIR bundle;
  bundle README) — `py/clear-text-storage-sensitive-data`
- `src/anastomosis/deliver/browser/persist.py` (upload manifest) —
  `py/clear-text-storage-sensitive-data`
- `src/anastomosis/deliver/render_index.py` (render-index sidecar) —
  `py/clear-text-storage-sensitive-data`
- `src/anastomosis/deliver/fhir_api/destination.py` (a log line carrying
  the *name* of the matched field, never a value) —
  `py/clear-text-logging-sensitive-data`

A policy test pins this list: every inline suppression in `src/` must sit
beside a `PHI-BY-DESIGN` rationale and appear in the list above, so a new
suppression cannot land without amending this policy. Every module not
listed here remains fully covered by both rules; if the field-name
convention drifts or a writer lands outside a hardened directory, CodeQL
alerts again.

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
