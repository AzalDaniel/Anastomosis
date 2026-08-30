#!/usr/bin/env bash
# The full local gate, exactly what CI runs. Any stage failing fails the run —
# pipefail because a `| tail` must never mask a failing gate.
set -euo pipefail
cd "$(dirname "$0")/.."

python tools/preflight.py
ruff check .
ruff format --check .
python -m mypy
# `pytest`, not `python -m pytest`: the `-m` form puts the working directory on
# sys.path and CI's bare `pytest` does not, so the two disagreed about what was
# importable and this script's first line stopped being true.
pytest
python tools/complexity_gate.py
python tools/phi_scan.py
echo "ALL GATES GREEN"
