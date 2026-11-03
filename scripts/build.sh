#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ -x .venv/bin/python ]] || { echo "Run bash scripts/setup.sh first." >&2; exit 1; }
[[ -f .env ]] || { echo "Copy .env.example to .env and configure your device." >&2; exit 1; }
.venv/bin/ab build brain --brain brain.abdesign.json --env .env "$@"
