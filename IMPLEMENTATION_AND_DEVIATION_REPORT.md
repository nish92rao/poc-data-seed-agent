# Final Implementation And Deviation Report

Date: 2026-09-25
Component: `poc-data-seed`
Application version: `0.1.0`
Deployed validator image: `1.0.6`
Next validator image: `1.0.11` (implemented and locally verified; deployment pending)
Status: implementation and acceptance complete

## Executive Summary

`poc-data-seed` is a non-user-facing Magenta/LangGraph agent invoked by the Coding Orchestrator with a POC identifier. It resolves approved design artifacts from shared MongoDB state, reads those artifacts from exact GitHub commits, normalizes Draft Agent output, generates an immutable versioned seed bundle, validates the exact committed bundle through an HMAC-authenticated Lambda, performs bounded autonomous repairs for implementation failures, commits a validation report, and publishes the latest validated `seed.js` pointer back to shared state through a strict compare-and-set update.

The final accepted run used the minimal request `{"request":{"poc_id":"1790237138344"}}`, generated `seed/v013`, validated successfully, and published the shared-state seed pointer. No run-scoped validation database remained afterward.

## Implemented Features And Evidence

### Minimal Invocation Contract

Only `request.poc_id` is required. Numeric strings and prefixed ULIDs are supported. Valid caller correlation and limit fields are retained; missing or malformed optional fields receive safe normalized values.

Owners:

- `src/agent_poc_data_seed/envelope.py`
- `src/agent_poc_data_seed/state.py`
- `src/agent_poc_data_seed/main.py`

Evidence:

- `tests/test_envelope.py`
- Final Magenta UI invocation accepted only `poc_id` and produced fresh `run_id`, `task_id`, and `trace_id` values.

### Shared-State Resolution And Publication

The agent loads one document by exact `pov_id` from the configured shared-state collection. It consumes only `spec_artifacts.data_model` and the prior `spec_artifacts.seed` pointer. Query-pattern metadata is ignored. Duplicate POC documents, malformed data-model references, cross-repository URLs, path traversal, and unavailable MongoDB are rejected before LLM generation.

On complete success, the agent compare-and-set updates only:

```text
spec_artifacts.seed
updated_at
```

The compare-and-set filter binds the exact data-model artifact object and the prior seed pointer or its absence. Query-pattern changes do not block publication. Concurrent authoritative changes cannot be overwritten.

Owners:

- `src/agent_poc_data_seed/shared_state.py`
- `src/agent_poc_data_seed/shared_context.py`
- `src/agent_poc_data_seed/main.py::load_shared_context_node`
- `src/agent_poc_data_seed/main.py::final_result_node`

Evidence:

- `tests/test_shared_state.py`
- `tests/test_shared_context.py`
- Unknown POC acceptance returned `POC_NOT_FOUND` before generation.
- A disposable malformed shared document returned `SHARED_STATE_INVALID` and retained no seed pointer.
- Final accepted pointer:

```json
{
  "path": "seed/v013/seed.js",
  "commit_sha": "0afe541c2d5535caeb4de56017765f2c4ee7eaf9",
  "url": "https://github.com/nish92rao/magenta-test-repo/blob/nishit-rao-mongodb-com/triage-support/seed/v013/seed.js"
}
```

### GitHub Storage And Immutable Layout

The repository is fixed by `GITHUB_REPO`; the project branch is validated from the data-model URL. The data model is read from its exact commit. Outputs are confined to `seed/vNNN/` and reports to the run-scoped validation subdirectory.

```text
spec_architect/
  data_model.json

seed/vNNN/
  seed.js
  package.json
  SEED_README.md
  REPAIR_NOTES.md                       # repairs only
  seed.manifest.json
  validation/{run_id}/report.json
```

Bundle files and manifest are committed atomically with the Git Data API. Ref updates are non-force and guarded by the expected branch head. Exact-commit read-back verifies manifest identity, artifact set, byte counts, and SHA-256 values.

Owners:

- `src/agent_poc_data_seed/github_storage.py`
- `src/agent_poc_data_seed/artifact_layout.py`
- `src/agent_poc_data_seed/github_tool_calls.py`
- `src/agent_poc_data_seed/tools.py`

Evidence:

- `tests/test_github_storage.py`
- `tests/test_workflow.py`
- `tests/test_github_commit_preflight.py`
- Final bundle commit: `0afe541c2d5535caeb4de56017765f2c4ee7eaf9`.
- Final report commit and branch HEAD: `e6471db1a17da5afbd0502a406fb7eb6c27de371`.

### Draft Artifact Normalization

Production consumes only `data_model.json` from `spec_architect/`. The normalizer converts Draft Agent structures into a deterministic internal contract:

- `document_shape` maps become canonical field records;
- relationship metadata is ignored regardless of dialect;
- omitted seed counts receive documented deterministic defaults;
- only explicit ordinary indexes are retained;
- field types and required flags are canonicalized for recursive runtime validation.

Every applied default is recorded in `seed.manifest.json` and `SEED_README.md`.

Owners:

- `src/agent_poc_data_seed/shared_context.py`
- `src/agent_poc_data_seed/system_message.py`

Evidence:

- `tests/test_shared_context.py`
- Query-pattern presence and contents do not affect normalization.

### Seed Generation Security Contract

Generated `seed.js` targets Node.js 20 and the official MongoDB driver. Pre-commit validation requires all runtime variables and `seed_summary`, restricts package behavior, restricts direct modules, rejects arbitrary process/environment access, and blocks dynamic execution. Lambda repeats stronger AST-based validation with `acorn`.

Owners:

- `src/agent_poc_data_seed/seed_contract.py`
- `resources/validator/artifact_content_validator.js`

Evidence:

- `tests/test_seed_contract.py`
- `resources/validator/artifact_content_validator.test.js`
- `tests/test_github_commit_preflight.py`

### Lambda Validation

The image-based Lambda accepts exact committed contents inline through `POST /v1/validations/direct`. It has no GitHub token and performs no repository writes.

Validation covers:

- request authentication and timestamp window;
- manifest storage, input, artifact, and correlation integrity;
- package and JavaScript syntax/security policy;
- deterministic capped execution in a run-scoped database;
- seed summary and cap agreement;
- explicit ordinary indexes and recursive required/optional field, enum, nested schema, and BSON type validation;
- zeroed query/search validation fields for response compatibility;
- run-scoped database deletion in `finally`.

Owners:

- `src/agent_poc_data_seed/validator_client.py`
- `resources/validator/handler.js`
- `resources/validator/artifact_content_validator.js`

Evidence:

- `resources/validator/handler.test.js`
- `resources/validator/artifact_content_validator.test.js`
- Real-cluster create/list/drop search-index lifecycle passed.
- Final response reported `runtime_executed: 7`, `static_vector_validated: 1`, and the expected temporary vector index.
- Independent post-run check found no run-scoped validation database.

Validator `1.0.11` establishes the field-only data-model contract while preserving exact source bytes and commits. Query patterns and relationship metadata are ignored. Runtime validation covers generated-code security, package/syntax, seed caps/summary, explicit data-model indexes, required/optional field presence, nullability, enums, nested schemas, BSON types, and mandatory cleanup. Cloud deployment is pending.

### HMAC And Endpoint Controls

The direct route is public HTTPS but authenticated with HMAC-SHA256 over `timestamp.canonical_json_body`. Lambda validates a 300-second timestamp window with timing-safe comparison. Secret caching is bounded and refreshes once on mismatch for rotation. Replay inside the window is accepted intentionally.

API Gateway exposes only:

```text
GET /health
POST /v1/validations/direct
```

The stage is throttled at 5 requests/second with burst 10. Access logs exclude request bodies and headers.

Owners:

- `src/agent_poc_data_seed/validator_client.py`
- `resources/validator/handler.js`
- `resources/aws/start-seed-validator.sh`

Evidence:

- Deployed Lambda `1.0.6`: state `Active`, update status `Successful`.
- Five live endpoint/GitHub tests passed.
- `SECURITY_REVIEW.md` records the complete security evidence.

### Autonomous Repair

Only implementation failures enter repair. Each repair creates the next immutable `vNNN`, reads the prior exact bundle, receives a sanitized validator finding, and writes a complete replacement plus `REPAIR_NOTES.md`. One invocation permits at most three validation attempts. Security, integrity, contradiction, authorization, protocol, infrastructure, and shared-state failures do not invoke LLM repair.

Owners:

- `src/agent_poc_data_seed/workflow.py`
- `src/agent_poc_data_seed/main.py`
- `src/agent_poc_data_seed/memory.py`

Evidence:

- `tests/test_workflow.py`
- Repair exhaustion and immutable version advancement were exercised during live migration testing.

### Memory And Observability

Durable checkpoints, Magenta memory, and shared POC state remain separate. Timeline events record correlation, versions, attempts, decisions, safe artifact metadata, and terminal status. Memory writes fail open and do not control workflow success.

Owners:

- `src/agent_poc_data_seed/observability.py`
- `src/agent_poc_data_seed/memory.py`
- `src/agent_poc_data_seed/response.py`

Evidence:

- `tests/test_observability.py`
- `tests/test_memory.py`
- `tests/test_response.py`

## Terminal-Path Evidence

| Terminal path | Expected behavior | Evidence |
|---|---|---|
| Success | Commit bundle, validate exact commit, commit report, verify both, CAS-publish seed pointer, return `succeeded`. | Magenta UI `v013`; exact MongoDB/GitHub/report read-back passed. |
| POC not found | Return non-retryable `POC_NOT_FOUND` before LLM/GitHub generation. | Live acceptance completed in 288 ms with only `workflow_failed`. |
| Invalid shared state | Return non-retryable `SHARED_STATE_INVALID`; do not write seed pointer. | Disposable cross-repository POC acceptance passed and document was removed. |
| Shared-state unavailable | Return retryable `SHARED_STATE_UNAVAILABLE`; no GitHub generation or publication. | Unit failure-injection coverage in `tests/test_shared_state.py`. |
| Shared-state CAS conflict | Return retryable `SHARED_STATE_CONFLICT`; preserve winner and leave orphan Git version auditable. | `tests/test_shared_state.py`; exact prior-pointer CAS assertions. |
| Request contradiction | Return non-retryable `REQUEST_CONTRADICTION`; do not invoke repair. | `tests/test_shared_context.py`, `tests/test_workflow.py`. |
| Repairable implementation failure | Create next immutable version and retry, up to three validation attempts. | `tests/test_workflow.py`; live migration repair loops. |
| Repair exhausted | Return `REPAIR_ATTEMPTS_EXHAUSTED`; shared pointer unchanged. | `tests/test_workflow.py`; live pre-acceptance runs. |
| Security/integrity failure | Stop without repair/publication; return stable security/integrity code. | Seed AST/preflight and manifest tampering tests. |
| Validator infrastructure/protocol failure | Stop without LLM repair; return retryable infrastructure/protocol code where defined. | Validator client/workflow tests and live deployment-mismatch tests. |
| Cleanup failure | Return retryable `VALIDATION_CLEANUP_FAILED`; sanitize driver/URI details. | Node cleanup failure tests. |
| Deadline/token limit | Stop before another phase with `DEADLINE_EXCEEDED` or `TOKEN_BUDGET_EXCEEDED`. | `tests/test_limits.py`. |

No terminal failure updates `spec_artifacts.seed`.

## Test Coverage And Evidence

Final cleaned-repository automated results:

- 98 Python `unittest` tests passed.
- 40 Node validator tests passed.
- Golden capped MongoDB integration passed under the 60-second threshold.
- Two-cluster validation/target isolation proof passed.
- Five deployed GitHub/Lambda live tests passed.
- Final Magenta UI generation passed.
- Editor diagnostics and `git diff --check` were clean.

Commands:

```bash
scripts/test-unit.sh
scripts/test-security.sh
INTEGRATION_MONGODB_URI='mongodb://127.0.0.1:27017/?directConnection=true' scripts/test-integration.sh
scripts/test-security-isolation.sh
RUN_LIVE_TESTS=1 scripts/test-live.sh
```

The test taxonomy, prerequisites, and explicit markers are documented in `README.md`.

## Remaining Risks

### Accepted Or Controlled Risks

- HMAC requests can replay within 300 seconds. The route is capped, run-scoped, cleanup-bound, throttled, and immutable-write guarded; no nonce store is provisioned.
- GitHub commit and MongoDB publication are not a distributed transaction. If GitHub/report succeed but final MongoDB CAS fails, the invocation fails, shared state remains unchanged, and the immutable GitHub version becomes an auditable orphan. The next invocation consumes another version.
- Atlas Search index readiness is asynchronous. Validation creates and verifies the index definition but does not execute `$vectorSearch`; ordinary queries execute at runtime.
- LLM generation quality remains probabilistic. Deterministic pre-commit policy, capped validation, and bounded repair contain the risk.
- Failed immutable versions accumulate in GitHub. Retention/archival policy is a platform operation, not an agent mutation.
- HMAC/API throttling reduces abuse but does not replace a private network or stronger caller identity.

### Deployment Risks Requiring Operational Control

- `SEED_VALIDATION_MONGODB_URI` must use credentials isolated from target POC databases.
- `SHARED_STATE_MONGODB_URI` currently reuses validation credentials in local development by explicit temporary decision; production must use separate least-privilege shared-state credentials.
- Updating `.env` requires full Agent Engine stack recreation; container restart alone preserves stale Docker environment values.
- GitHub PAT, HMAC secret, LLM keys, and MongoDB URIs require external rotation and secret injection.
- Cleanup failure is surfaced, but there is no scheduled orphan-database/index scavenger in this repository.
- No hosted CI workflow is configured; repository scripts are CI-compatible entry points.

## Intentional Deviations From The Original Specification

Reference: `resources/POC_Builder_High_Level_Specification.md`.

| Original specification | Final implementation | Reason |
|---|---|---|
| S3 versioned asset layout and pre-signed URLs | GitHub exact-commit storage; no S3/PAR path | Atomic multi-file commits, immutable audit history, exact-SHA reads, and removal of presigned URL exposure. |
| `schema_design.json` input | `spec_architect/data_model.json` only | Data model is the sole authoritative seed-generation and validation input. |
| Artifacts under `pocs/{poc_id}/code/vNNN/seed/` | Artifacts under branch-local `seed/vNNN/` | The POC is isolated by validated project branch; downstream agents use shared-state pointer plus exact commit SHA. |
| `poc_id`-keyed `pocs` document | Exact lookup by string `pov_id` | Matches the actual shared platform document inserted for integration. Numeric strings and prefixed ULIDs are supported. |
| Rich agent request including versions, mode, paths, and repair context | Only `request.poc_id` is required | Shared state and GitHub are authoritative; graph-owned normalization prevents caller/model identity drift. |
| Coding Orchestrator supplies code/spec versions and repair mode | Agent discovers the next GitHub version and performs bounded internal repair | Keeps immutable version allocation and validator findings inside one controlled workflow. |
| Coding Orchestrator/platform publishes latest asset metadata | Agent publishes only `spec_artifacts.seed` after full success | Prevents downstream deployment from observing an unvalidated seed bundle. |
| Separate stage/run/task documents drive the agent | Agent returns correlation/timeline; platform owns external runs/tasks | Avoids duplicating platform run orchestration inside this component. |
| No Secrets Manager in the original v1 platform | Validator HMAC secret is stored in AWS Secrets Manager | Required for authentication of the public direct-validation endpoint. |
| Validation mainly executes ordinary MongoDB behavior | Vector query runtime is statically validated; Lambda creates/verifies/deletes the temporary search index | Atlas Search readiness is asynchronous and unsuitable for the synchronous validation window. |
| Generated code security deferred | AST and artifact security validation implemented | Required before executing untrusted generated JavaScript against any MongoDB credential. |
| Platform MongoDB URI may coexist with application state | Dedicated `SHARED_STATE_MONGODB_URI` | Avoids coupling application shared state to Magenta platform internals. |

## Explicitly Out-Of-Scope Platform Responsibilities

### Coding Orchestrator / Platform

- Create and own the POC document, unique `pov_id`, approvals, status, run/task records, and broader platform state.
- Produce and approve `data_model.json` and write its exact GitHub reference into shared state. Query patterns are outside this agent's contract.
- Invoke this agent with at least `request.poc_id` and stop downstream progression on `response.status != "succeeded"`.
- Persist platform-level run/task outcomes and expose them to user-facing services.
- Coordinate API and frontend generation and assemble the complete deployable code bundle.
- Decide platform retry/backoff policy for retryable terminal failures.
- Monitor orphan immutable GitHub versions and define retention policy.
- Provision hosted CI if desired.

### Deploy Agent / Infrastructure Platform

- Provision or validate the actual target MongoDB cluster/database and credentials.
- Ensure validation credentials cannot access target POC databases.
- Read `spec_artifacts.seed.path` at `spec_artifacts.seed.commit_sha`, not mutable branch HEAD.
- Download `seed.js`, `package.json`, and accompanying bundle files from the same exact seed commit.
- Supply target `MONGODB_URI`, `DB_NAME`, and deployment-time `SEED_MAX_DOCS` to `seed.js`.
- Permit production Atlas Search index creation and wait for readiness when the deployed application requires `$vectorSearch`.
- Handle EC2/container deployment, target database loading, application build/start/health, retries, rollback, expiry, and teardown.
- Configure network access lists/VPC/security groups and secret injection.
- Monitor Lambda/API Gateway/Atlas operational metrics and cleanup alerts.

### Draft Agent

- Produce coherent data-model and query-pattern artifacts.
- Provide complete field names, types, required flags, nested schemas, and enums; relationship metadata is ignored by this agent.
- Prefer explicit seed counts, indexes, and vector dimensions; deterministic defaults exist only to keep valid but incomplete POV metadata executable.

## Migration And Compatibility Decisions

- S3/PAR support is a hard cutover and has no production compatibility path.
- The Lambda retains the historical pinned golden direct-validation contract for regression coverage while supporting the final project layout.
- Historical golden fixtures remain under `tests/golden`; they are test-only and are not production inputs.
- Local fixture/generated tools and `resources/fixtures`, `resources/generated`, and `resources/manual-loads` were removed.
- The production tool sandbox exposes exactly four tools:
  - `read_seed_input_from_github_tool`
  - `commit_seed_bundle_to_github_tool`
  - `read_seed_repair_source_from_github_tool`
  - `validate_github_seed_bundle_tool`
- Full legacy envelopes remain parseable for valid correlation and limits, but caller-provided workflow/storage/version fields are intentionally ignored. Shared state and GitHub control production behavior.
- Missing correlation IDs receive fresh run-scoped ULIDs persisted in durable graph state.
- Seed pointer commit semantics intentionally use the bundle commit, not the later report commit. The report commit is returned separately.
- Failed GitHub versions are consumed and never overwritten.
- Numeric-string POC identifiers and prefixed ULIDs are both accepted.
- Current application version remains `0.1.0`; validator deployment version is `1.0.6`.

## Coding Orchestrator Operational Requirements

### Invocation

Minimum request:

```json
{
  "request": {
    "poc_id": "1790237138344"
  }
}
```

Optional valid `run_id`, `task_id`, `trace_id`, deadline, and token budget may be supplied for platform correlation. Do not include credentials or attempt to control branch, path, source commits, versions, validation mode, or repair source.

### Success Handling

Proceed only when:

```text
response.status == "succeeded"
response.error == null
response.result.validation.status == "succeeded"
```

Use:

- `response.result.source_commit_sha` as the immutable seed bundle commit;
- `response.result.report_commit_sha` as validation audit commit;
- `response.result.code_version` as the accepted immutable version;
- `response.result.validation.report_key` as the report path;
- shared-state `spec_artifacts.seed` as the platform's latest validated seed pointer.

The Orchestrator should verify the returned `poc_id` and correlation values before recording completion.

### Failure Handling

- Do not start deployment when the response is failed.
- Inspect `error.code`, `error.retryable`, and `error.detail.failure_class`.
- Retry the same minimal request only when the error is retryable or after the responsible platform/input defect is corrected.
- Do not manually reuse a failed `vNNN`; the agent allocates the next unused version.
- Do not construct external repair envelopes. Implementation repair is internal and bounded.
- `POC_NOT_FOUND`, `SHARED_STATE_INVALID`, `REQUEST_CONTRADICTION`, integrity, and security failures require correction before reinvocation.
- Infrastructure, GitHub rate/availability, protocol, cleanup, and CAS conflict failures may be reinvoked according to platform backoff policy.

## Deploy Agent Operational Requirements

1. Read the current POC document by exact `pov_id`.
2. Require `spec_artifacts.seed.path`, `.commit_sha`, and `.url`.
3. Enforce the configured GitHub repository and branch derived from the URL.
4. Read `seed.js` from `spec_artifacts.seed.path` at the exact seed commit SHA.
5. Read `package.json`, `SEED_README.md`, and `seed.manifest.json` from the same `seed/vNNN/` directory and commit.
6. Verify manifest hashes and bytes before execution.
7. Install with the committed lock/package policy and run `node seed.js` with target database environment variables.
8. Use the target POC database, never the shared-state or validation database.
9. For production vector search, allow the seed script to create the declared search index; do not set `SEED_SKIP_SEARCH_INDEXES=1` outside validation.
10. Wait for production search-index readiness before executing application `$vectorSearch` queries.
11. Record seed summary and source commit in deployment metadata.
12. On seed failure, return a structured deployment failure to the Coding Orchestrator and do not proceed to backend/frontend startup.

## Operational Configuration

Required agent settings:

```text
SHARED_STATE_MONGODB_URI
SHARED_STATE_DATABASE
SHARED_STATE_COLLECTION
GITHUB_USERNAME
GITHUB_TOKEN
GITHUB_REPO
SEED_VALIDATOR_URL
SEED_VALIDATOR_HMAC_SECRET
SEED_VALIDATOR_TIMEOUT_SECONDS
SEED_VALIDATION_MONGODB_URI
OPENAI_API_KEY / provider equivalent
VOYAGE_API_KEY when memory is enabled
```

Changing local `.env` requires full stack recreation:

```bash
agentengine dev down
AGENTENGINE_DEV_WATCH=0 agentengine dev up
```

A container restart alone retains stale Docker environment values.

## Handoff Checklist

- [x] Shared sample POC and exact GitHub specification inputs bootstrapped.
- [x] GitHub immutable project layout implemented and verified.
- [x] Shared-state exact lookup and success-only CAS publication implemented.
- [x] Minimal request and stable response envelopes implemented.
- [x] Autonomous repair and terminal failure routing implemented.
- [x] Lambda validator `1.0.6` deployed with correct routes.
- [x] Temporary vector-index and validation-database cleanup verified.
- [x] Security review completed.
- [x] Unit, validator, integration, isolation, live, and UI acceptance passed.
- [x] Final READMEs revised.
- [x] Legacy fixtures/generated/manual-load artifacts and dead local tools removed.

## References

- `README.md` — final runtime, operation, testing, and deployment contract.
- `SECURITY_REVIEW.md` — security findings, controls, residual risks, and evidence.
- `resources/POC_Builder_High_Level_Specification.md` — original platform specification used for deviation analysis.
- `env.example` — complete environment-variable template.
- `agent.yaml` — final four-tool sandbox registration and runtime feature flags.
- `resources/aws/` — validator deployment, environment export, and teardown.
- `resources/validator/README.md` — Lambda-specific request, validation, cleanup, test, and build contract.
