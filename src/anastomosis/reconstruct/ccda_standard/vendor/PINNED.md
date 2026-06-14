# Vendored HL7 CDA stylesheet — pin record

These files are vendored **unmodified** from HL7's official C-CDA stylesheet so
the standard C-CDA render path has no network dependency at runtime.

- **Repository:** https://github.com/HL7/cda-core-xsl
- **License:** Apache License, Version 2.0
- **Pinned tag:** `v4.1.0-beta.2`

The pin is `>= 4.0.2-beta10`, the version that fixed the embedded-content XSS
class (CVE-2014-3861 / CVE-2014-5452 lineage) by mandating iframe sandboxing;
`v4.1.0-beta.2` is a superset that carries all prior security fixes. The render
path additionally runs the transform under `lxml`'s
`XSLTAccessControl(read_network=False)` and leaves the stylesheet's `limit-pdf`
and `limit-external-images` parameters at their secure `'yes'` defaults.

## Files (raw.githubusercontent.com/HL7/cda-core-xsl/v4.1.0-beta.2/)

| File | Role | SHA-256 |
| --- | --- | --- |
| `CDA.xsl` | main stylesheet (XSLT 1.0) | `f1c32f11186cf69d64f874cc4c94eb51ca8f696b54b4b6833fb54c6afdd7acc1` |
| `cda_l10n.xml` | localization strings (loaded via `document()`) | `d8677885ef78afc0842484ecc70a65f8f17eea286e990bd39fa948cac7f1a884` |
| `cda_narrativeblock.xml` | narrative-block attribute whitelist (loaded via `document()`) | `608b518182a1418e8865e5e8437083bad034762cf078750ec27c10d17a2285a9` |

## Re-vendoring (verifiable)

```sh
TAG=v4.1.0-beta.2
BASE=https://raw.githubusercontent.com/HL7/cda-core-xsl/$TAG
for f in CDA.xsl cda_l10n.xml cda_narrativeblock.xml; do curl -sSLo "$f" "$BASE/$f"; done
sha256sum -c <<'EOF'
f1c32f11186cf69d64f874cc4c94eb51ca8f696b54b4b6833fb54c6afdd7acc1  CDA.xsl
d8677885ef78afc0842484ecc70a65f8f17eea286e990bd39fa948cac7f1a884  cda_l10n.xml
608b518182a1418e8865e5e8437083bad034762cf078750ec27c10d17a2285a9  cda_narrativeblock.xml
EOF
```

`CDA.xsl` is XSLT 1.0, so `lxml`/`libxslt` runs it natively. The two XML files
must remain co-located with `CDA.xsl` so its `document()` calls resolve.
