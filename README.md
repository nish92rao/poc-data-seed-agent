# poc-data-seed

Magenta Data Seeding Agent for the POC Builder platform. The Coding Orchestrator invokes this non-user-facing agent with a POC identifier. The agent resolves approved design inputs from shared MongoDB state, generates an immutable MongoDB seed bundle on the POC's GitHub branch, validates the exact committed content through Lambda, repairs implementation failures within a bounded budget, and publishes the latest successful seed pointer back to shared state.

## Final Architecture

```text
Coding Orchestrator
  -> AgentEnvelope { request: { poc_id } }
  -> shared MongoDB lookup by pov_id
  -> exact GitHub data_model/query_patterns reads
  -> LLM generation with graph-owned tool arguments
  -> atomic seed/vNNN GitHub commit
  -> signed Lambda validation against a disposable database
  -> validation report GitHub commit
  -> exact read-back and MongoDB compare-and-set publication
  -> AgentEnvelope response
```

The LLM never chooses the repository, branch, source commits, output version, write paths, correlation IDs, or expected branch head. The graph resolves and normalizes those values before tool execution.

## GitHub Layout

The repository is fixed by `GITHUB_REPO`. The branch is derived from the two validated GitHub blob URLs in shared state; both inputs must use the same branch and the configured repository.

```text
spec_architect/
  data_model.json
  query_patterns.json

seed/
  v001/
    seed.js
    package.json
    SEED_README.md
    seed.manifest.json
    validation/{run_id}/report.json
  v002/
    seed.js
    package.json
    SEED_README.md
    REPAIR_NOTES.md              # repair versions only
    seed.manifest.json
    validation/{run_id}/report.json
  ...
```

Each generation or repair uses the next unused `vNNN`, beginning at `v001`. Bundle files and the manifest are committed atomically. Validation reports are committed separately as children of the bundle commit. Reads use exact commit SHAs; branch HEAD is used only for optimistic writes. Existing immutable paths cannot be changed. Failed or partially published versions remain auditable and are never reused.

The canonical report path is `seed/{code_version}/validation/{run_id}/report.json`.

`spec_artifacts.seed.commit_sha` points to the bundle commit where `seed.js` was committed. `report_commit_sha` is returned separately and is normally the branch HEAD after successful validation.

S3, pre-signed URLs, and Lambda-side GitHub credentials are not used.

## Shared State

Configuration:

```dotenv
SHARED_STATE_MONGODB_URI=mongodb+srv://...
SHARED_STATE_DATABASE=poc_builder
SHARED_STATE_COLLECTION=pocs
```

The collection contains one document per POC, uniquely identified by the string field `pov_id`. Numeric strings and prefixed ULIDs are supported.

Required input shape:

```json
{
  "pov_id": "1790237138344",
  "spec_artifacts": {
    "data_model": {
      "path": "spec_architect/data_model.json",
      "commit_sha": "<40-character SHA>",
      "url": "https://github.com/owner/repo/blob/branch/spec_architect/data_model.json"
    },
    "query_patterns": {
      "path": "spec_architect/query_patterns.json",
      "commit_sha": "<40-character SHA>",
      "url": "https://github.com/owner/repo/blob/branch/spec_architect/query_patterns.json"
    }
  }
}
```

The agent projects only `pov_id` and `spec_artifacts`; unrelated shared-state fields are neither sent to the LLM nor returned to callers. Input paths, repositories, branches, and commit SHAs are strictly validated. Each input may use a different exact source commit.

After complete success, the agent compare-and-set updates only `spec_artifacts.seed` and `updated_at`:

```json
{
  "spec_artifacts": {
    "seed": {
      "path": "seed/v013/seed.js",
      "commit_sha": "<seed bundle commit>",
      "url": "https://github.com/owner/repo/blob/branch/seed/v013/seed.js"
    }
  }
}
```

The compare-and-set filter includes the exact input references and prior seed pointer loaded at run start. Concurrent upstream changes return `SHARED_STATE_CONFLICT` instead of overwriting newer state. Any generation, validation, GitHub read-back, report, cleanup, or MongoDB publication failure leaves the shared seed pointer unchanged. A GitHub version created before final publication failure remains an immutable orphan and the next invocation consumes a new version.

Bootstrap the sample POC inputs once with:

```bash
set -a
source .env
set +a
.venv/bin/python scripts/bootstrap-shared-poc.py 1790237138344
```

The bootstrap command enforces the unique `pov_id` index, creates the shared-state branch when absent, atomically commits `resources/data_model.json` and `resources/query_patterns.json`, performs exact GitHub read-back, compare-and-set updates only the two input references, and verifies MongoDB read-back. It does not modify the local source inputs.

## Request Envelope

Only `request.poc_id` is required:

```json
{
  "request": {
    "poc_id": "1790237138344"
  }
}
```

Valid caller-provided `run_id`, `task_id`, `trace_id`, `deadline_at`, and `budget.max_tokens` are retained. Missing or malformed optional values are normalized safely:

- fresh prefixed ULIDs for `run_id` and `task_id`;
- `trace_<run ULID>` for `trace_id`;
- no effective deadline when none is supplied;
- token budget `1,000,000`;
- generate-and-validate mode with at most three validation attempts.

Caller-provided storage modes, branches, paths, versions, source commits, validation modes, and repair sources do not control production behavior. Secrets anywhere in the envelope are rejected.

Direct OE invocation wraps the serialized envelope in `message` and supplies top-level `user_id`:

```bash
OE_PORT="$(docker compose -f .agentengine/docker-compose.dev.yml port oe 8000 | awk -F: '{print $NF}')"

curl -X POST "http://127.0.0.1:${OE_PORT}/invoke" \
  -H 'Content-Type: application/json' \
  -d '{"message":"{\"request\":{\"poc_id\":\"1790237138344\"}}","user_id":"local-test"}'
```

Use the OE port shown by `agentengine dev up`; it may differ between runs.

## Response Envelope

Successful responses follow the platform envelope:

```json
{
  "response": {
    "task_id": "task_...",
    "status": "succeeded",
    "result": {
      "poc_id": "1790237138344",
      "run_id": "run_...",
      "code_version": "v013",
      "target_database_name": "1790237138344",
      "source_commit_sha": "<seed bundle commit>",
      "report_commit_sha": "<report commit>",
      "validation": {
        "status": "succeeded",
        "caps": {"collection": 1},
        "seed_summary": {"collection": 1},
        "query_validation": {
          "runtime_executed": 7,
          "static_vector_validated": 1
        },
        "search_index_validation": {
          "created": [{"collection": "collection", "name": "index_name"}]
        },
        "report_key": "seed/v013/validation/run_.../report.json"
      },
      "artifact_metadata": [],
      "timeline": {"events": [], "attempted_versions": ["v013"]}
    },
    "artifacts": [
      {"kind": "code", "key": "seed/v013/seed.js", "version": "v013"},
      {"kind": "report", "key": "seed/v013/validation/run_.../report.json", "version": "v013"}
    ],
    "error": null,
    "usage": {"input_tokens": 0, "output_tokens": 0, "duration_ms": 0}
  }
}
```

Failure responses use `status: failed`, keep safe correlation/version/timeline data in `result`, return no published artifacts unless they are independently verified, and include:

```json
{
  "error": {
    "code": "POC_NOT_FOUND",
    "message": "No POC was found for the supplied poc_id.",
    "retryable": false,
    "detail": {
      "failure_class": "REQUEST_VALIDATION_FAILURE",
      "validation_attempts": 0
    }
  }
}
```

## Input Normalization

Production consumes Draft Agent `data_model.json`, not the historical `schema_design.json` contract. The adapter:

- converts `document_shape` maps to canonical field records;
- preserves types, required flags, nested definitions, relationships, descriptions, examples, and enums;
- applies a deterministic capped seed-count default where omitted;
- derives relationship and ordinary query indexes only for known source fields;
- rejects unknown collections/fields and contradictory relationships;
- converts keyed `QP-*` query objects into canonical patterns;
- records every applied default in `seed.manifest.json` and `SEED_README.md`.

For `$vectorSearch`, the agent uses a documented deterministic synthetic-vector dimension when the Draft Agent omitted dimensions. Ordinary queries execute during validation. Vector query contracts are statically validated because Atlas Search index readiness is asynchronous.

## Seed Script Contract

Generated `seed.js` targets Node.js 20 and the official `mongodb` driver. It must:

- read `MONGODB_URI`, `DB_NAME`, `SEED_MAX_DOCS`, `SEED_COLLECTION_CAPS`, and optional validation-only `SEED_SKIP_SEARCH_INDEXES`;
- obey independent per-collection caps and the total document budget;
- deterministically recreate POC collections, values, ObjectIds, and synthetic vectors;
- create every declared ordinary index and relationship;
- declare the matching Atlas Vector Search index where required;
- guard generated search-index creation when `SEED_SKIP_SEARCH_INDEXES=1`;
- support every normalized query pattern;
- print exactly one final JSON line: `{"seed_summary":{"collection":1}}`;
- exit non-zero on failure.

`package.json` may declare only `mongodb` and exactly the `seed` script. `seed.js` may directly require only `mongodb`, `crypto`, or `node:crypto`. Dynamic execution, aliased/nonliteral module loading, arbitrary network/filesystem/process modules, literal credentials, and undeclared environment/process access are rejected before immutable GitHub writes and again in Lambda.

## Validation And Repair

The normal lifecycle is:

1. Normalize the request and load the POC by exact `pov_id`.
2. Validate shared-state references and read exact GitHub input commits.
3. Normalize and cross-check the data model and query patterns.
4. Allocate the next unused `seed/vNNN`.
5. Generate and preflight the complete bundle.
6. Atomically commit the bundle and manifest.
7. Reread and hash-verify the exact bundle commit.
8. Send original input bytes, normalized execution structures, manifest, and artifacts to `POST /v1/validations/direct`.
9. Validate in a capped run-scoped database.
10. Commit `seed/vNNN/validation/{run_id}/report.json`.
11. On success, verify bundle/report commits and compare-and-set publish shared state.

Repair is autonomous and only applies to `IMPLEMENTATION_FAILURE` results. The graph increments to the next unused immutable version, supplies the sanitized validator finding, reads the prior bundle, and requires a complete replacement bundle with `REPAIR_NOTES.md`. At most three validation attempts occur in one invocation. Integrity, security, authorization, protocol, infrastructure, contradiction, and shared-state failures do not enter LLM repair.

The Coding Orchestrator should stop when the response is failed. It may re-invoke the minimal request when `error.retryable` is true or after an operator corrects a non-retryable input/security defect. Failed versions are not overwritten.

## Validator And Report Contract

Lambda is validation-only. It has no GitHub token and performs no artifact-store writes. The agent sends exact committed contents inline and commits the returned report.

Validation includes:

- package, syntax, AST security, size, secret, hash, byte-count, manifest, storage, input, and correlation checks;
- capped deterministic seed execution and exact `seed_summary` verification;
- ordinary index, relationship, and query-pattern execution;
- static vector contract verification;
- temporary Atlas Vector Search index creation and discovery by Lambda;
- explicit deletion of each declared temporary search index;
- disposable validation database deletion in `finally`.

Collection caps are:

$$
\min(\lceil 0.05 \times \text{declared count} \rceil, 100)
$$

The total `SEED_MAX_DOCS` is the sum of independent collection caps. Search-index or database cleanup failures return retryable `VALIDATION_CLEANUP_FAILED` and never expose credentials.

## Metadata And Correlation

`seed.manifest.json` contains:

- `poc_id`, `code_version`, and producer;
- GitHub repository and branch;
- original `data_model` and `query_patterns` path, source commit SHA, and SHA-256;
- generated artifact path, SHA-256, and byte count;
- deterministic defaults applied during normalization;
- `poc_id`, `run_id`, `task_id`, `trace_id`, and producer correlation;
- repair lineage when applicable.

Reports repeat source commit and correlation identity. Timeline events include correlation IDs, version, phase, validation attempt, decisions, artifact metadata, terminal status, and bounded safe details. Artifact bodies, prompts, raw stderr, tokens, connection strings, and provider response bodies are excluded.

## Terminal Failure Taxonomy

| Failure class | Stable codes | Retry behavior |
|---|---|---|
| `REQUEST_VALIDATION_FAILURE` | `INVALID_REQUEST`, `POC_NOT_FOUND`, `SHARED_STATE_INVALID` | Correct the request/shared document; not retryable as-is. |
| `REQUEST_CONTRADICTION` | `REQUEST_CONTRADICTION` | Draft Agent requirements are nonsensical or internally inconsistent; correct inputs first. |
| `IMPLEMENTATION_FAILURE` | `VALIDATION_FAILED`, `REPAIR_ATTEMPTS_EXHAUSTED`, `ARTIFACT_MALFORMED`, `PACKAGE_JSON_INVALID`, `SEED_SCRIPT_INVALID`, `ARTIFACT_SIZE_EXCEEDED` | Repairable failures are handled internally up to the limit; terminal exhaustion requires reinvocation or input correction. |
| `PLATFORM_INFRASTRUCTURE_FAILURE` | `SHARED_STATE_UNAVAILABLE` | Retryable. |
| `VALIDATOR_INFRASTRUCTURE_FAILURE` | `VALIDATION_TIMEOUT`, `VALIDATION_CLEANUP_FAILED`, `VALIDATOR_INFRASTRUCTURE_FAILURE` | Retryable after infrastructure recovery. |
| `VALIDATOR_PROTOCOL_FAILURE` | `VALIDATOR_INVALID_RESPONSE` | Retryable after client/deployment correction. |
| `AUTHORIZATION_FAILURE` | `UNAUTHORIZED`, `GITHUB_AUTH_FAILED` | Correct credentials/configuration; not automatically repairable. |
| `ARTIFACT_CONFLICT` | `SHARED_STATE_CONFLICT`, `CODE_VERSION_ALREADY_EXISTS`, `ARTIFACT_EXISTS`, `GITHUB_ARTIFACT_EXISTS`, `GITHUB_CONFLICT` | Shared/GitHub races may be retryable; immutable collisions require a new version. |
| `ARTIFACT_INTEGRITY_FAILURE` | `ARTIFACT_HASH_MISMATCH`, `ARTIFACT_SIZE_MISMATCH`, `GITHUB_CONTENT_MISMATCH`, `GITHUB_MANIFEST_INVALID` | Do not repair or publish; investigate source/commit integrity. |
| `ARTIFACT_SECURITY_FAILURE` | `ARTIFACT_SECRET_DETECTED`, `SEED_SCRIPT_SECURITY_VIOLATION` | Do not publish; correct generated content/policy violation. |
| `ARTIFACT_TRANSFER_FAILURE` | `GITHUB_NOT_FOUND`, `GITHUB_RATE_LIMITED`, `GITHUB_UNREACHABLE`, `GITHUB_API_ERROR`, `GITHUB_INVALID_RESPONSE` | Retry only when the returned `retryable` flag is true. |
| `EXECUTION_LIMIT` | `DEADLINE_EXCEEDED`, `TOKEN_BUDGET_EXCEEDED` | Supply appropriate valid limits or start a new run. |

Unknown internal failures are normalized to `VALIDATOR_INFRASTRUCTURE_FAILURE`. Public messages are sanitized and capped.

## Secret Handling

Never put secrets in AgentEnvelope, shared artifact metadata, generated files, prompts, responses, memory, or logs.

- `GITHUB_TOKEN` is consumed only by deterministic GitHub client code in the agent/tool runtimes. It is never placed in prompts, model-visible tool results, responses, or Lambda requests.
- `SEED_VALIDATOR_HMAC_SECRET` signs the exact canonical request body plus timestamp; Lambda reads its copy from AWS Secrets Manager.
- `SEED_VALIDATION_MONGODB_URI` is passed only through protected runtime code to Lambda and generated validation process environment.
- `SHARED_STATE_MONGODB_URI` remains environment-only. Production should use a separate least-privilege credential that can read the POC document and update only the owned seed pointer/timestamp fields.
- The validation MongoDB account must be isolated from target POC databases.
- `MONGODB_URI` is platform-owned by local Agent Engine services unless `dev.yaml` selects external platform MongoDB.
- Error sanitization removes URIs, credentials, AWS ARNs, network addresses, stack paths, raw stderr, and provider bodies.

The public validator endpoint uses HTTPS. `GET /health` is unauthenticated. `POST /v1/validations/direct` requires HMAC-SHA256 headers `x-validator-timestamp` and `x-validator-signature`. Signatures cover `timestamp.canonical_json_body`; timestamps outside 300 seconds are rejected. Replay inside that bounded window is accepted by design. API Gateway throttles requests and access logs omit bodies and headers.

## Configuration

Copy `env.example` to `.env` and configure:

```dotenv
SHARED_STATE_MONGODB_URI=mongodb+srv://...
SHARED_STATE_DATABASE=poc_builder
SHARED_STATE_COLLECTION=pocs

OPENAI_API_KEY=...
OPENAI_BASE_URL=...
OPENAI_MODEL=...
VOYAGE_API_KEY=...

GITHUB_USERNAME=...
GITHUB_TOKEN=...
GITHUB_REPO=owner/repository

SEED_VALIDATOR_URL=https://...
SEED_VALIDATOR_HMAC_SECRET=...
SEED_VALIDATOR_TIMEOUT_SECONDS=24
SEED_VALIDATION_MONGODB_URI=mongodb+srv://...
```

Changing `.env` while the local stack is running requires full environment regeneration; container restart alone preserves stale Docker environment values:

```bash
agentengine dev down
AGENTENGINE_DEV_WATCH=0 agentengine dev up
```

## Local Development

Prerequisites: Python 3.11+, Node.js 20, Docker, npm, and the Agent Engine CLI.

```bash
agentengine dev down
AGENTENGINE_DEV_WATCH=0 agentengine dev up
agentengine dev status
```

The Playground is available at `http://localhost:3000`. OE and MongoDB host ports are printed by `agentengine dev up` and may be dynamically assigned by Docker.

## Test Commands And Markers

Default credential-free tests:

```bash
scripts/test-unit.sh
```

This runs Python `unittest` discovery, Node validator tests, and shell syntax checks.

Security regressions:

```bash
scripts/test-security.sh
```

Local capped golden integration:

```bash
INTEGRATION_MONGODB_URI='mongodb://127.0.0.1:27017/?directConnection=true' \
  scripts/test-integration.sh
```

The integration runner requires `INTEGRATION_MONGODB_URI`, installs locked golden Node dependencies, uses a unique disposable database, validates counts/indexes/relationships/queries, reruns deterministically, and enforces the 60-second capped-validation target. Set `ALLOW_NONLOCAL_INTEGRATION_MONGODB=1` only for an intentional non-local endpoint.

Combined local checks:

```bash
INTEGRATION_MONGODB_URI='mongodb://127.0.0.1:27017/?directConnection=true' \
  scripts/test-all-local.sh
```

Two-cluster destructive-isolation proof:

```bash
set -a
source .env
set +a
scripts/test-security-isolation.sh
```

This requires distinct `SECURITY_VALIDATION_MONGODB_URI` and `SECURITY_TARGET_MONGODB_URI` values and cleans both disposable fixtures.

Real GitHub/Lambda suite:

```bash
set -a
source .env
set +a
RUN_LIVE_TESTS=1 scripts/test-live.sh
```

`RUN_LIVE_TESTS=1` is mandatory. The suite requires GitHub and validator settings, uses a disposable smoke branch for write proof, reads a pinned golden bundle, checks health/auth/replay/retired routes, invokes signed validation, and does not mutate the permanent golden branch.

There is intentionally no hosted CI workflow. These scripts are portable CI entry points. Magenta Cloud deployment is outside this repository's current scope.

## Lambda Deployment

The current deployment script provisions image-based Lambda validator `1.0.6` in `ap-south-1`. Review the resource prefix, region, tags, expiry, and image tag at the top of `resources/aws/start-seed-validator.sh` before staging or production use.

From AWS CloudShell, place `resources/aws/start-seed-validator.sh` beside `resources/validator/`, then run:

```bash
bash resources/aws/start-seed-validator.sh
```

The script:

- builds and pushes a Linux/AMD64 ECR image;
- creates or updates the Lambda, least-privilege role, HMAC secret, logs, and HTTP API;
- configures 4096 MB memory, 25-second Lambda timeout, and 10 GB ephemeral storage;
- exposes only `GET /health` and `POST /v1/validations/direct`;
- removes retired validation/presign routes;
- configures API throttling, body-free access logs, and 14-day retention;
- writes `nishrao-poc-data-seed-validator-lambda-state.json` for scoped teardown.

Retrieve local validator settings after deployment:

```bash
bash resources/aws/print-validator-local-env.sh
```

That prints `SEED_VALIDATOR_URL`, `SEED_VALIDATOR_HMAC_SECRET`, and timeout. Configure `SEED_VALIDATION_MONGODB_URI` separately; it is never stored in Lambda or printed by the script.

After changing local `.env`, recreate the Agent Engine stack, then run `RUN_LIVE_TESTS=1 scripts/test-live.sh` before acceptance testing.

Teardown:

```bash
bash resources/aws/teardown-seed-validator.sh
```

Teardown targets only resources recorded in the namespaced state file. ECR and the HMAC secret are retained unless their explicit deletion flags are enabled.

## Staging Checklist

1. Configure separate shared-state and validation MongoDB credentials.
2. Ensure the shared collection has a unique `pov_id` index.
3. Insert or verify the POC shared document and its two exact GitHub input references.
4. Run `scripts/bootstrap-shared-poc.py <pov_id>` only when fixture input publication is required.
5. Deploy the current Lambda image and verify state `Active`, update `Successful`, and the two-route inventory.
6. Update `.env`, then fully recreate the Agent Engine stack.
7. Run unit, security, integration, isolation, and live suites with their explicit markers.
8. Submit `{"request":{"poc_id":"<pov_id>"}}` through the Playground or Coding Orchestrator.
9. Verify response success, exact bundle/report commits, shared-state pointer, and no remaining validation database/search index.
10. Verify unknown POC and controlled malformed-state failures leave `spec_artifacts.seed` unchanged.

## Memory And Observability

Durable LangGraph checkpoints, Magenta memory, and shared POC state have distinct responsibilities:

- checkpoints preserve in-run graph execution;
- memory stores sanitized run episodes and successful repair patterns;
- shared state publishes only the latest validated seed pointer.

Memory failures are fail-open and never determine workflow success. Lifecycle events are emitted as compact `seed_timeline_event` JSON logs and returned as a bounded timeline. Event IDs are deterministic within a run and include correlation, version, phase, attempt, decision, safe artifact metadata, and terminal status.

The threat model, final security findings, accepted bounded replay risk, removed S3/PAR mapping, and validation/target database isolation proof are documented in [SECURITY_REVIEW.md](SECURITY_REVIEW.md).
