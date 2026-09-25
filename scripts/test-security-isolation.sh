#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

: "${SECURITY_VALIDATION_MONGODB_URI:?Set SECURITY_VALIDATION_MONGODB_URI to the dedicated validation cluster URI}"
: "${SECURITY_TARGET_MONGODB_URI:?Set SECURITY_TARGET_MONGODB_URI to the separate target cluster URI}"

npm --prefix tests/golden/subscription_billing/v013 ci --ignore-scripts --no-audit --no-fund
node --test tests/integration/database_isolation.test.js