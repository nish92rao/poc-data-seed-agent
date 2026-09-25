# Final Security Review

Date: 2026-09-25

## Scope And Trust Boundaries

The production path resolves exact specification references from shared MongoDB state and uses GitHub for immutable specification, versioned seed, and validation-report artifacts. The agent reads exact commit SHAs and sends exact artifact text to the public Lambda validator over HTTPS with an HMAC-signed request. Lambda executes generated JavaScript against a capped, run-scoped database on a dedicated validation MongoDB cluster. Shared state is updated only after exact bundle/report read-back through a strict source-and-pointer compare-and-set.

Generated `seed.js` is untrusted. GitHub contents, public HTTP requests, model-issued tool arguments, and provider error bodies are also untrusted. Agent/tool environment variables, the HMAC secret in AWS Secrets Manager, and separately permissioned validation MongoDB credentials are trusted configuration.

## Requirement Disposition

| Requirement | Disposition | Control and evidence |
|---|---|---|
| Prefix containment and cross-POC/cross-run access | Fixed and tested | Configured repository, validated shared-state branch, exact `spec_architect` inputs, immutable `seed/vNNN` scope, graph-owned identities, run-scoped report keys, stale-head rejection, exact-SHA reads, and percent/traversal rejection. |
| Pre-signed URL scope and expiry | Removed | S3/PAR modules and routes were deleted. Regression tests prove no active presign implementation or route is created. |
| HMAC signing and replay window | Fixed with accepted risk | Strict 64-character hex HMAC-SHA256, exact body binding, timing-safe comparison, ±300-second window, bounded secret cache, refresh after rotation mismatch. Replay inside 300 seconds remains accepted by design and is tested. |
| S3 metadata/hash integrity | Migrated | Atomic Git commits plus manifest SHA-256 and byte counts replace S3 metadata. Exact data-model, query, storage, POC, version, and artifact identities are verified. |
| Secret scanning and error sanitization | Fixed and tested | Artifact scans cover URIs, credentialed URLs, private keys, and secret assignments. AST policy limits modules and environment access. Public failures redact credentials, URIs, ARNs, network addresses, stack paths, and stderr. |
| Validator cleanup scope | Fixed and tested live | Cleanup selects only the computed `validationDatabaseName(poc_id, run_id)`, explicitly deletes declared temporary Atlas Search indexes, and drops the disposable database. List/drop/close failures return sanitized retryable `VALIDATION_CLEANUP_FAILED`. |
| Validation cannot modify actual target database | Configuration-backed proof complete | Dedicated validation and target clusters/accounts are mandatory. The two-cluster sentinel suite proved that validation credentials cannot modify the target-cluster sentinel. Code alone cannot prove operator configuration, so endpoint separation remains a deployment control. |
| Query-pattern and relationship scope | Removed in validator `1.0.11`; deployment pending | Query patterns and relationship metadata are ignored. Validation enforces data-model field presence, nullability, enums, BSON types, explicit indexes, caps, security, and cleanup. |

## Findings

### SEC-001: Noncanonical GitHub Paths

- Severity: High
- Status: Fixed, locally verified
- Fix: Reject non-ASCII paths, control characters, percent-bearing/double-encoded branches, backslashes, empty segments, `.` and `..`, cross-repository URLs, and paths outside exact specification/versioned-seed scopes.
- Evidence: `tests/test_github_storage.py` through `scripts/test-security.sh`.

### SEC-002: Incomplete Manifest Identity Binding

- Severity: High
- Status: Fixed, locally verified
- Fix: Bind read bundles to the configured repository, shared-state branch, canonical `spec_architect` input paths, exact per-input commits, versions, SHA-256 values, byte counts, correlation, and artifact set.
- Evidence: Python GitHub tests and Node direct-validation tampering tests.

### SEC-003: HMAC Parsing And Rotation

- Severity: High
- Status: Fixed and deployed; live verification passed
- Fix: Strict hex parsing, deterministic verifier, exact ±300-second checks, five-minute cache TTL, and one forced secret refresh after mismatch.
- Residual risk: A captured valid request can replay inside 300 seconds. This is accepted because validation is capped, run-scoped, cleanup-bound, and immutable report writes are idempotent/conflict checked. API throttling limits abuse. No nonce store is provisioned.

### SEC-004: Generated JavaScript Inspection

- Severity: Critical
- Status: Fixed and deployed in validator `1.0.6`; pinned golden and shared-state-generated validations passed
- Fix: `acorn` parses generated JavaScript. Only direct literal `require()` calls for `mongodb`, `crypto`, or `node:crypto` are accepted. Aliased/nonliteral loading, dynamic imports, dynamic execution, global network APIs, and environment variables outside the seed contract are rejected.
- Evidence: Validator unit tests, pinned golden validation, and successful shared-state-generated `seed/v013` validation.

### SEC-005: Silent Cleanup Failure

- Severity: High
- Status: Fixed and deployed; local cleanup-scope tests and live validation passed
- Fix: Exact run database cleanup is mandatory. Lambda creates/verifies the declared temporary vector index, deletes only declared search-index names, then drops the run database. A failed list/drop/database-drop/close produces `VALIDATION_CLEANUP_FAILED`, HTTP 503, and retryable infrastructure routing without exposing the URI or driver error.

### SEC-006: Target Database Isolation

- Severity: Critical
- Status: Fixed by architecture and verified against distinct validation and target endpoints
- Required control: `SEED_VALIDATION_MONGODB_URI` must identify a dedicated cluster/account whose credentials have no privileges on the actual target cluster.
- Proof: `scripts/test-security-isolation.sh` compares endpoint identities, creates a disposable target sentinel, attempts same-name destructive operations through validation credentials, verifies the target sentinel survives, and cleans both fixtures.
- Evidence: The proof passed against the configured dedicated validation endpoint and local target MongoDB in 0.83 seconds; the first overlong disposable-name attempt failed before destructive validation access and cleanup still ran, then the Atlas-compatible rerun passed.
- Limitation: Run-scoped `DB_NAME` prevents accidental targeting but cannot constrain a malicious MongoDB client on a shared privileged cluster. Separate credentials and endpoints are part of the security boundary.

### SEC-007: Legacy S3/PAR Reintroduction

- Severity: Medium
- Status: Removed and regression tested
- Fix: Tests assert removed modules, clients, dependencies, active runtime operations, IAM actions, bucket provisioning, and routes remain absent. The deployed live suite checks the former presign route returns 404.

### SEC-008: API Abuse And Logging

- Severity: High
- Status: Fixed, deployed, and inspected
- Fix: API Gateway default throttling is 5 requests/second with burst 10. Access logs contain only request ID, route key, and status; request bodies and headers are excluded. Lambda and API logs retain 14 days.
- Deployment evidence: Lambda state `Active`, update status `Successful`; API stage reports rate 5 and burst 10; route inventory contains only `GET /health` and `POST /v1/validations/direct`.

### SEC-009: Query-Pattern Bypass

- Severity: High
- Status: Removed and locally verified in validator `1.0.11`; cloud deployment pending
- Fix: Production consumes only `data_model.json`. Query-pattern references, files, payloads, hashes, validation, writes, and Search-index lifecycle are bypassed. Legacy query metadata is accepted only for manifest compatibility and ignored. Query-validation counters are always zero.
- Evidence: Shared-state bypass tests, workflow single-read tests, malformed/missing query-payload tests, data-model-only golden integration, and mandatory cleanup tests.

## Verification Commands

Credential-free security regressions:

```bash
cd /Users/nishit.rao/Documents/Learning/AI/magenta-test/poc-data-seed/agents/poc-data-seed
scripts/test-security.sh
```

Two-cluster isolation proof after setting the two environment-only URIs:

```bash
cd /Users/nishit.rao/Documents/Learning/AI/magenta-test/poc-data-seed/agents/poc-data-seed
set -a
source .env
set +a
scripts/test-security-isolation.sh
```

Full local regression:

```bash
cd /Users/nishit.rao/Documents/Learning/AI/magenta-test/poc-data-seed/agents/poc-data-seed
INTEGRATION_MONGODB_URI='mongodb://127.0.0.1:27017/?directConnection=true' scripts/test-all-local.sh
```

Deployed live verification:

```bash
cd /Users/nishit.rao/Documents/Learning/AI/magenta-test/poc-data-seed/agents/poc-data-seed
set -a
source .env
set +a
RUN_LIVE_TESTS=1 scripts/test-live.sh
```

## Closure Gates

The security review closes only after the current Lambda image, route/throttle/log/IAM controls, two-cluster proof, automated suites, shared-state failure immutability, and a minimal-envelope Magenta UI generation all pass.

## Final Acceptance Evidence

All closure gates passed on 2026-09-25:

- Hardened Lambda `1.0.6` was active with a successful update.
- API Gateway exposed only `GET /health` and `POST /v1/validations/direct`, with rate 5 requests/second, burst 10, and body-free access logs.
- Final cleaned-repository regression: 98 Python tests and 40 Node validator tests passed.
- Deterministic capped golden validation and two-cluster isolation passed.
- Expanded deployed live suite: 5 tests passed.
- Two-cluster sentinel proof passed; validation-cluster destructive operations did not alter the target-cluster sentinel.
- Final Magenta Playground generation accepted only `poc_id`, produced and validated `seed/v013`, executed seven ordinary queries, statically validated one vector query, created and deleted the temporary vector index, and exposed no secret or connection value.
- Shared state published `seed/v013/seed.js` at seed commit `0afe541c2d5535caeb4de56017765f2c4ee7eaf9`; validation report commit `e6471db1a17da5afbd0502a406fb7eb6c27de371` became branch HEAD.
- Exact bundle/report read-back passed and no run-scoped validation database remained.
- Unknown-POC and malformed shared-state acceptance cases returned structured failures with no GitHub generation or seed-pointer update.
