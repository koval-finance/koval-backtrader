#!/usr/bin/env bash
# The definition of done for this repository: every gate CI runs, in one
# command. The exit code is the entire contract.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${KOVAL_VERIFY_PYTHON:-python3}"
if [ -z "${KOVAL_VERIFY_PYTHON:-}" ] && [ -x .venv/bin/python ]; then
    PYTHON=.venv/bin/python
fi

"$PYTHON" -m ruff check .
"$PYTHON" -m ruff format --check .
"$PYTHON" -m pytest -q
