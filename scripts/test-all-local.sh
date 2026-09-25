#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

scripts/test-unit.sh
scripts/test-integration.sh
