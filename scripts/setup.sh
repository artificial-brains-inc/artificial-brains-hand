#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10 or newer is required"'
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
[[ -f .env ]] || cp .env.example .env
.venv/bin/python run.py --check
printf '\nEdit .env, identify the two serial devices, then build and run as shown in README.md.\n'
