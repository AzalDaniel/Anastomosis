# Third-party material redistributed with Anastomosis

Anastomosis itself is licensed AGPL-3.0-or-later (see `LICENSE`). The
artifacts it ships — the Python wheel and the Windows installer — also
redistribute the third-party works below. Each entry names the work, its
copyright holder, its license, and where the full license text ships in
this repository and in the built artifacts.

The full license texts live in [`assets/licenses/`](assets/licenses/) and are
packaged into the wheel (`*.dist-info/licenses/`) and the Windows installer
(`{app}\licenses\`).

## HL7 CDA R2 Stylesheet — Apache License 2.0

| File | Role |
| --- | --- |
| `src/anastomosis/reconstruct/ccda_standard/vendor/CDA.xsl` | C-CDA → human-readable rendering |
| `src/anastomosis/reconstruct/ccda_standard/vendor/cda_l10n.xml` | localization strings for CDA.xsl |
| `src/anastomosis/reconstruct/ccda_standard/vendor/cda_narrativeblock.xml` | narrative-block vocabulary for CDA.xsl |

- Copyright: HL7 Structured Documents Work Group and contributors
  (foundational work by HL7 Germany and Finland (Tyylitieto) and Calvin
  Beebe, HL7 US; presentation approach by Tony Schaller, medshare GmbH;
  subsequent maintenance by Lantana Group (US) and Nictiz (NL)).
- License: Apache License, Version 2.0 — full text at
  [`assets/licenses/APACHE-2.0.txt`](assets/licenses/APACHE-2.0.txt).
- Source: <https://github.com/HL7/cda-core-xsl> (unmodified upstream
  artifacts; exact pinned tag and checksums in
  `src/anastomosis/reconstruct/ccda_standard/vendor/PINNED.md`; attribution
  detail in the adjacent `NOTICE`).

## Mona Sans — SIL Open Font License 1.1

| File | Role |
| --- | --- |
| `src/anastomosis/gui/web/fonts/MonaSansVF.woff2` | GUI interface typeface (variable) |

- Copyright (c) 2023, GitHub <https://github.com/github/mona-sans>, with
  Reserved Font Name "Mona Sans".
- License: SIL Open Font License, Version 1.1 — full text at
  [`assets/licenses/OFL-1.1.txt`](assets/licenses/OFL-1.1.txt).

## JetBrains Mono — SIL Open Font License 1.1

| File | Role |
| --- | --- |
| `src/anastomosis/gui/web/fonts/JetBrainsMonoVF.woff2` | GUI monospace typeface (variable) |

- Copyright 2020 The JetBrains Mono Project Authors
  (<https://github.com/JetBrains/JetBrainsMono>).
- License: SIL Open Font License, Version 1.1 — full text at
  [`assets/licenses/OFL-1.1.txt`](assets/licenses/OFL-1.1.txt).

## Provenance discipline

Every binary file in this repository is additionally hash-approved in
`tools/phi_scan.py`'s allowlist (`tools/phi_allowlist.txt`) with a
provenance comment; an unlisted or modified binary fails the repository
scan. Runtime *dependencies* (installed from PyPI, not redistributed in
this repository) are licensed as declared in their own distributions; the
compatibility review lives in `DESIGN.md`.
