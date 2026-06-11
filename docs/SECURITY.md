# Security Policy

## Threat model in one paragraph

Anastomosis processes Protected Health Information (PHI) **locally**. The
core pipeline makes no network calls; nothing is transmitted, telemetered, or
phoned home. The assets to protect are: the source export, the reconstructed
documents, the tracking database, error screenshots, and any credentials the
*operator's own browser session* holds. The threat surface is therefore the
local machine, the output directories, and any pack (plugin) code you choose
to run.

## Operator responsibilities

- Run on an access-controlled machine with full-disk encryption.
- Treat every output directory (archives, rendered PDFs, screenshots,
  `tracking.db`) as PHI at rest. The tool creates them with restrictive
  permissions and drops a warning README inside; keeping them safe after
  that is on you.
- **Never paste real patient data into GitHub issues.** Reproduce bugs with
  the synthetic fixtures under `tests/fixtures/`.
- You (or your practice) are the HIPAA covered entity / business associate.
  The Anastomosis authors are not a business associate and no BAA exists.

## Tool guarantees

- Zero network calls in the core pipeline (ingest/reconstruct/QA/archive);
  delivery paths that *do* talk to an EHR (API push, browser automation) say
  so explicitly and only talk to the destination you configure.
- Credentials are never stored by the tool. Browser delivery attaches to a
  Chrome session *you* logged into (CDP on loopback only); API delivery reads
  credentials from your environment/config, never writes them.
- Logs and error messages are designed not to carry patient names; error
  screenshots can carry PHI by nature and are written to a protected
  directory with a documented retention recommendation.
- Third-party packs are executable code. Built-in packs are reviewed here;
  anything else requires your explicit opt-in (`--pack-dir` / config trust
  entry). Review pack code before running it against real data.

## PHI in this repository

None, enforced: `tools/phi_scan.py` (hashed deny-list + SSN/GUID/phone/DOB
pattern checks) runs in pre-commit and CI on the full tree. Synthetic data
conventions: GUIDs start `feedface-` or `00000000-`, phones use the 555
exchange, SSNs use never-issued ranges.

## Reporting a vulnerability

Open a GitHub security advisory (preferred) or a private report to the
maintainer. Please do not file public issues for exploitable problems.
You'll get an acknowledgment within 7 days.
