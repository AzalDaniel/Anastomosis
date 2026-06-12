# Contributing to Anastomosis

Thank you — this tool only matters if it works for *every* practice, and that
takes many hands.

## Rule #1 (non-negotiable): no real patient data. Ever. Anywhere.

- All fixtures must be synthetic: Synthea-generated, or hand-built following
  the conventions in `tests/fixtures/README.md` (`feedface-` GUIDs, 555
  phones, never-issued SSN ranges).
- `tools/phi_scan.py` runs in pre-commit and CI. If it flags your change,
  fix the data — only add to `tools/phi_allowlist.txt` for genuine false
  positives, with a justification comment.
- Never paste real chart contents into issues, PRs, or commit messages.
- Porting note: parts of this codebase are re-typed refactors of a private
  production system. The hashed deny-list (`tools/phi_hashes.json`) exists to
  block that system's identifiers from ever following the code here. Do not
  copy files from private deployments into this repo.

## Workflow

1. `pip install -e ".[dev]" && pre-commit install`
2. Branch, write code **with tests**, keep `ruff`, `mypy`, and `pytest` green.
3. Sign off your commits (DCO): `git commit -s`. There is no CLA — you keep
   your copyright; the project stays AGPL-3.0 forever.
4. Open a PR. Small, focused PRs review faster.

## What contributions help most

- **Source adapters** (`sources/`): each EHR's EHI export format, built from
  the vendor's public documentation (cite it in the module docstring).
- **Template packs** (`packs/`): faithful layouts for more EHR note styles.
- **Destination packs** (`destinations/`): delivery into more systems —
  `anast destination init` scaffolds one and walks you through selector
  capture.
- **Capability registry updates** (`destinations/registry.yaml`): every entry
  must carry a source URL and verified date. No capability claims without
  evidence — this project does not hallucinate vendor features.
- Bug reports with synthetic reproductions.

## Architecture ground rules

- Packs are isolated modules: your change to one pack must not be able to
  break another (the registry loads defensively; keep it that way).
- The core pipeline makes no network calls. Anything that talks to the
  outside world lives in `deliver/` or an explicitly-named `live` module.
- Preserve the engine lessons in `docs/rendering-engine.md` — they were paid
  for with weeks of production debugging.
- New code is `mypy --strict` clean and carries tests; parsers should get
  property-based tests where practical.
