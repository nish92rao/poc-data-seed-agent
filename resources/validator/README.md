# Seed Validator Lambda

This Lambda validates exact GitHub-committed seed bundle contents supplied inline by the Data Seeding Agent. It does not access GitHub, S3, or any other artifact store, and it does not persist validation reports.

## Routes

- `GET /health`: public health check.
- `POST /v1/validations/direct`: HMAC-authenticated synchronous validation.

All other routes return `404`.

## Authentication

The validation route requires:

- `x-validator-timestamp`: Unix timestamp within 300 seconds.
- `x-validator-signature`: lowercase HMAC-SHA256 of `timestamp.canonical_json_body`.

The HMAC secret is read from AWS Secrets Manager through `VALIDATOR_AUTH_SECRET_ARN`. The endpoint is publicly routable but validation requests are authenticated. Replay within the timestamp window is accepted by design.

## Request

The direct request contains production correlation IDs, code version, repository and validated shared-state branch identity, the exact seed commit SHA, exact manifest text, original `data_model` and `query_patterns` bytes, normalized execution structures, exact generated artifact text, and a runtime-injected MongoDB URI. Each specification input is bound to its own path, commit SHA, and content hash in the manifest. The request never contains a GitHub token.

The handler validates storage identity, correlation, canonical `spec_architect` and `seed/vNNN` paths, hashes, byte counts, static security constraints, JavaScript syntax, package constraints, seed output, ordinary indexes, relationships, and query patterns. Ordinary queries execute against a capped run-scoped database. For Atlas Vector Search, Lambda creates and verifies the declared temporary vector index, records static vector-query validation, explicitly deletes that index, and then drops the disposable database. Cleanup failures return sanitized retryable infrastructure errors.

## Environment

```text
VALIDATOR_AUTH_SECRET_ARN
VALIDATION_TEST_FORCE_IMPLEMENTATION_FAILURE=false
```

The MongoDB URI is supplied in the authenticated request by the trusted tool runtime and is never logged or returned.

## Test

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm test
```

## Build

```bash
docker buildx build --platform linux/amd64 --load -t poc-data-seed-validator .
```
