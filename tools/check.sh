#!/usr/bin/env bash
# The full local gate, exactly what CI runs. Any stage failing fails the run —
# pipefail because a `| tail` must never mask a failing gate.
set -euo pipefail
cd "$(dirname "$0")/.."

ruff check .
ruff format --check .
python -m mypy
python -m pytest
python tools/phi_scan.py
echo "ALL GATES GREEN"
