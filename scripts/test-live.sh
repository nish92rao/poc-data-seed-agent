#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "${RUN_LIVE_TESTS:-}" != "1" ]]; then
  echo "Refusing shared-infrastructure tests: set RUN_LIVE_TESTS=1 explicitly." >&2
  exit 2
fi

: "${GITHUB_USERNAME:?Set GITHUB_USERNAME}"
: "${GITHUB_TOKEN:?Set GITHUB_TOKEN}"
: "${GITHUB_REPO:?Set GITHUB_REPO}"
: "${SEED_VALIDATOR_URL:?Set SEED_VALIDATOR_URL}"
: "${SEED_VALIDATOR_HMAC_SECRET:?Set SEED_VALIDATOR_HMAC_SECRET}"
: "${SEED_VALIDATION_MONGODB_URI:?Set SEED_VALIDATION_MONGODB_URI}"

PYTHONPATH=src .venv/bin/python -m unittest tests/live/test_live_infrastructure.py
