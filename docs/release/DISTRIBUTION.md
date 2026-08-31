# Distribution and trust

How Anastomosis reaches a physician's machine without a warning standing in
front of it, decided once and recorded here rather than in an issue that
scrolls away. Research basis, from issue #293: Windows runs three separate
warning systems (SmartScreen reputation, Defender detection, browser download
marks); reputation accrues per file-hash for unsigned artifacts — reset on
every build — and per certificate for signed ones, compounding across
releases; and EV certificates lost their instant-reputation privilege in
2024, so OV-class is the ceiling worth paying for. Ours can be free.

## The decided path

1. **Microsoft Store via MSIX** — the packaging lane is in the tree
   (`packaging/build_msix.py`, the manifest template, the workflow wiring in
   `windows-package.yml`), additive beside the Inno EXE path. The Store
   re-signs MSIX submissions with Microsoft's certificate on every release, so
   Store installs never see SmartScreen — alphas and the v1.0 build alike.
   The codebase is MSIX-clean by audit: every runtime write goes to
   `~/.anastomosis`, `%LOCALAPPDATA%\Anastomosis`, or a user-chosen output
   directory; nothing writes into the install dir.
2. **SignPath Foundation for the GitHub-Releases EXE** — free OV signing for
   open source; this repository qualifies (AGPL-3.0, no dual licensing,
   active, released). No renewal cost ever: the Foundation operates the
   certificate and each release is submitted for signing. Accepted trade-offs:
   the publisher string reads "SignPath Foundation", and their attribution
   line goes on the download page. The CI integration lands in the isolated
   `release` environment once the application is approved.
3. **Store MSI/EXE path second** — the same listing can also carry the Inno
   installer, but that path requires a CA-signed installer, so it unlocks the
   day SignPath signs the EXE.
4. **winget after signing** — one manifest PR to `microsoft/winget-pkgs`,
   auto-updated per release; deferred until the EXE is signed so the package
   does not re-surface the warnings this plan exists to remove.
   **Chocolatey: skipped** — moderation overhead for an audience this
   product's users are not in.
5. **Microsoft Security Intelligence submissions: reactive only** — per-hash,
   no SLA, no API; used only if a release ever trips a Defender false
   positive.

## The two steps only the owner can take

Everything below is account identity, which no repository change can supply:

- **Partner Center**: free individual registration (ID + selfie
  verification), reserve the app name, paste the two Product-identity strings
  (`Identity/Name`, `Publisher`) into the manifest placeholders, upload,
  submit.
- **SignPath**: apply at signpath.org with this repository's URL; on
  approval, add the one repository secret, and the signing step ships in the
  `release` environment.

## The macOS road (post-v1.0b)

Python stays. pywebview already backs onto Cocoa/WKWebView, the CLI is
portable, Nuitka builds mac app bundles, and PyMuPDF/Playwright/lxml ship
macOS wheels — a rewrite would buy nothing and cost everything. The port is
packaging work: Apple Developer Program ($99/yr, the only unavoidable fee in
this whole plan), codesign + notarize in CI, a DMG, and sandbox entitlements
only if an App Store build is wanted beside direct download. Windows-only
code (WebView2 bootstrap, DACL hardening, Inno) is already isolated behind
platform checks. Mobile is a different product shape; parked until there is
demand.

## Interim honesty (already shipping)

Release notes label the installer unsigned and carry `gh attestation verify`
instructions — provenance ("this workflow run produced these bytes") is what
can be checked today, not publisher identity. Download guidance says
"More info → Run anyway" and deliberately not the file-Properties Unblock
checkbox, which strips all zone protections — broader than needed.
