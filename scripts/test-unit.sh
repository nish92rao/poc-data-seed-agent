#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
npm --prefix resources/validator test
bash -n resources/aws/*.sh scripts/*.sh
