# Bundled fonts — SIL Open Font License 1.1

The GUI ships two variable fonts locally so the desktop window renders fully
offline (the strict, self-only CSP forbids any network fetch — `font-src 'self'`).
Both are licensed under the **SIL Open Font License, Version 1.1 (OFL-1.1)**,
which permits bundling and redistribution with attribution and the license text.

| File                    | Family         | Axes                       | Upstream source |
| ----------------------- | -------------- | -------------------------- | --------------- |
| `MonaSansVF.woff2`      | Mona Sans      | `wght`, `wdth`, `opsz`, `ital` (one file) | github / mona-sans |
| `JetBrainsMonoVF.woff2` | JetBrains Mono | `wght`                     | JetBrains / JetBrainsMono |

## Upstream / license URLs

- **Mona Sans** — GitHub, Inc. — https://github.com/github/mona-sans
  License: OFL-1.1, https://github.com/github/mona-sans/blob/main/LICENSE
- **JetBrains Mono** — JetBrains s.r.o. — https://github.com/JetBrains/JetBrainsMono
  License: OFL-1.1, https://github.com/JetBrains/JetBrainsMono/blob/master/OFL.txt

The OFL requires that the fonts not be sold by themselves, that any derivative
keep the license, and that the original copyright/attribution be retained — all
satisfied by shipping this notice alongside the binaries. The fonts are used
as-is (no glyph modification); `MonaSansVF.woff2` carries the italic axis, so
italics are rendered via `font-style: italic` on the same `@font-face`, not a
second file.

> Mona Sans is a trademark of GitHub, Inc.; JetBrains Mono is a trademark of
> JetBrains s.r.o. Trademark rights are not licensed under the OFL — the marks
> are referenced here only to identify the fonts.
