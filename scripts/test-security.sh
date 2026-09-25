#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHONPATH=src .venv/bin/python -m unittest \
  tests/test_envelope.py \
  tests/test_failures.py \
  tests/test_github_storage.py \
  tests/test_memory.py \
  tests/test_observability.py \
  tests/test_response.py \
  tests/test_security_surface.py \
  tests/test_validator_client.py \
  tests/test_workflow.py
npm --prefix resources/validator test
bash -n resources/aws/*.sh scripts/*.sh