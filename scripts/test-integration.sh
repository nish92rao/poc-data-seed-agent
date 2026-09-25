#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

: "${INTEGRATION_MONGODB_URI:?Set INTEGRATION_MONGODB_URI to a disposable-test-capable MongoDB endpoint}"
npm --prefix tests/golden/subscription_billing/v013 ci --ignore-scripts --no-audit --no-fund
node --test tests/integration/golden_acceptance.test.js
