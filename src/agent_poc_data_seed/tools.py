"""Tool definitions for poc-data-seed."""

from __future__ import annotations

import json

from agent_engine_sdk_langgraph import App

from agent_poc_data_seed.github_storage import (
    GitHubRepoStore,
    GitHubStoreError,
    build_project_seed_bundle_files,
)
from agent_poc_data_seed.shared_context import apply_query_indexes, normalize_data_model, normalize_query_patterns
from agent_poc_data_seed.seed_contract import SeedContractError, validate_seed_bundle_text
from agent_poc_data_seed.validator_client import SeedValidatorClient, ValidatorClientError

def _github_store() -> GitHubRepoStore:
    return GitHubRepoStore.from_environment()


def _github_failure(error: GitHubStoreError) -> str:
    return json.dumps(
        {
            "status": "failed",
            "error": {
                "code": error.code,
                "message": str(error),
                "retryable": error.retryable,
            },
        }
    )


def _parse_json_object(value: str, name: str) -> dict:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{name} must be a JSON object") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object")
    return parsed


def _correlation(poc_id: str, run_id: str, task_id: str, trace_id: str) -> dict[str, str]:
    return {
        "poc_id": poc_id,
        "run_id": run_id,
        "task_id": task_id,
        "trace_id": trace_id,
        "producer": "poc-data-seed",
    }


def register(app: App) -> None:
    """Register the production GitHub-backed seed tools."""

    @app.tool(is_local=False)
    def read_seed_input_from_github_tool(
        poc_id: str,
        source_commit_sha: str,
        artifact: str,
        branch: str = "",
        path: str = "",
        spec_version: str = "",
        query_patterns_commit_sha: str = "",
    ) -> str:
        """Read data_model or query_patterns from an exact GitHub commit."""
        filenames = {"schema_design": "schema_design.json", "data_model": "data_model.json", "query_patterns": "query_patterns.json"}
        if artifact not in filenames:
            raise ValueError("artifact must be data_model or query_patterns")
        try:
            result = (
                _github_store().read_project_file(path, source_commit_sha)
                if branch and path
                else _github_store().read_file(poc_id, f"pocs/{poc_id}/spec/{spec_version}/{filenames[artifact]}", source_commit_sha)
            )
        except GitHubStoreError as error:
            return _github_failure(error)
        try:
            content = json.loads(result.content)
        except json.JSONDecodeError as error:
            raise ValueError(f"{artifact} is not valid JSON") from error
        if artifact == "data_model":
            content, defaults = normalize_data_model(content)
            if branch and query_patterns_commit_sha:
                query_artifact = _github_store().read_project_file(
                    "spec_architect/query_patterns.json",
                    query_patterns_commit_sha,
                )
                normalized_queries, query_defaults = normalize_query_patterns(json.loads(query_artifact.content))
                content, index_defaults = apply_query_indexes(content, normalized_queries)
                defaults = [*defaults, *query_defaults, *index_defaults]
        elif artifact == "query_patterns" and branch:
            content, defaults = normalize_query_patterns(content)
        else:
            defaults = []
        return json.dumps(
            {
                "status": "succeeded",
                "key": result.path,
                "sha256": result.sha256,
                "bytes": result.bytes,
                "source_commit_sha": result.commit_sha,
                "content": content,
                "defaults_applied": defaults,
            }
        )

    @app.tool(is_local=False)
    def commit_seed_bundle_to_github_tool(
        poc_id: str,
        run_id: str,
        task_id: str,
        trace_id: str,
        spec_version: str,
        code_version: str,
        seed_js: str,
        package_json: str,
        seed_readme: str,
        expected_head_sha: str,
        spec_commit_sha: str = "",
        branch: str = "",
        data_model_commit_sha: str = "",
        query_patterns_commit_sha: str = "",
        defaults_json: str = "[]",
        repair_notes: str = "",
        repair_json: str = "",
    ) -> str:
        """Atomically commit a complete seed bundle and manifest to the POC branch."""
        repair = _parse_json_object(repair_json, "repair_json") if repair_json else None
        try:
            validate_seed_bundle_text(seed_js, package_json)
        except SeedContractError as error:
            return json.dumps(
                {
                    "status": "failed",
                    "error": {
                        "code": error.code,
                        "message": str(error),
                        "failure_class": "IMPLEMENTATION_FAILURE",
                        "retryable": True,
                    },
                }
            )
        try:
            store = _github_store()
            if branch:
                data_model = store.read_project_file("spec_architect/data_model.json", data_model_commit_sha)
                query = store.read_project_file("spec_architect/query_patterns.json", query_patterns_commit_sha)
                defaults = json.loads(defaults_json)
                if not isinstance(defaults, list) or not all(isinstance(item, str) for item in defaults):
                    raise ValueError("defaults_json must be an array of strings")
                files = build_project_seed_bundle_files(
                    repository=store._config.repository,
                    branch=branch,
                    poc_id=poc_id,
                    code_version=code_version,
                    seed_js=seed_js,
                    package_json=package_json,
                    seed_readme=seed_readme,
                    data_model_input={"key": data_model.path, "sha256": data_model.sha256, "commit_sha": data_model.commit_sha},
                    query_patterns_input={"key": query.path, "sha256": query.sha256, "commit_sha": query.commit_sha},
                    correlation=_correlation(poc_id, run_id, task_id, trace_id),
                    defaults_applied=defaults,
                    repair_notes=repair_notes or None,
                    repair=repair,
                )
                result = store.commit_project_files_atomic(
                    branch,
                    files,
                    f"seed({poc_id}): commit {code_version}",
                    expected_head_sha=expected_head_sha,
                )
                return json.dumps({
                    "status": "succeeded",
                    "repository": result.repository,
                    "branch": result.branch,
                    "commit_sha": result.commit_sha,
                    "artifacts": list(result.artifacts),
                    "idempotent": result.idempotent,
                })
            schema = store.read_file(
                poc_id,
                f"pocs/{poc_id}/spec/{spec_version}/schema_design.json",
                spec_commit_sha,
            )
            query = store.read_file(
                poc_id,
                f"pocs/{poc_id}/spec/{spec_version}/query_patterns.json",
                spec_commit_sha,
            )
            result = store.commit_seed_bundle(
                poc_id=poc_id,
                spec_version=spec_version,
                code_version=code_version,
                seed_js=seed_js,
                package_json=package_json,
                seed_readme=seed_readme,
                schema_input={"key": schema.path, "sha256": schema.sha256},
                query_patterns_input={"key": query.path, "sha256": query.sha256},
                correlation=_correlation(poc_id, run_id, task_id, trace_id),
                repair_notes=repair_notes or None,
                repair=repair,
                expected_head_sha=expected_head_sha or None,
            )
        except GitHubStoreError as error:
            return _github_failure(error)
        return json.dumps(
            {
                "status": "succeeded",
                "repository": result.repository,
                "branch": result.branch,
                "commit_sha": result.commit_sha,
                "artifacts": list(result.artifacts),
                "idempotent": result.idempotent,
            }
        )

    @app.tool(is_local=False)
    def read_seed_repair_source_from_github_tool(
        poc_id: str,
        spec_version: str,
        previous_code_version: str,
        source_commit_sha: str,
        branch: str = "",
    ) -> str:
        """Read a hash-verified prior seed bundle from an exact GitHub commit."""
        try:
            store = _github_store()
            bundle = (
                store.read_project_seed_bundle(
                    branch=branch,
                    poc_id=poc_id,
                    code_version=previous_code_version,
                    commit_sha=source_commit_sha,
                )
                if branch
                else store.read_seed_bundle(
                    poc_id=poc_id,
                    spec_version=spec_version,
                    code_version=previous_code_version,
                    commit_sha=source_commit_sha,
                )
            )
        except GitHubStoreError as error:
            return _github_failure(error)
        source = {path.rsplit("/", 1)[-1]: content for path, content in bundle.files.items() if not path.endswith("seed.manifest.json")}
        return json.dumps(
            {
                "status": "succeeded",
                "repository": bundle.repository,
                "branch": bundle.branch,
                "source_commit_sha": bundle.commit_sha,
                "head_sha": store.resolve_existing_branch(branch)["head_sha"] if branch else store.resolve_poc_branch(poc_id)["head_sha"],
                "manifest": bundle.manifest,
                "source": source,
            }
        )

    @app.tool(is_local=False)
    def validate_github_seed_bundle_tool(
        poc_id: str,
        run_id: str,
        task_id: str,
        trace_id: str,
        spec_version: str,
        code_version: str,
        source_commit_sha: str,
        branch: str = "",
    ) -> str:
        """Validate exact GitHub-committed contents and commit the report to the POC branch."""
        import os

        mongodb_uri = os.getenv("SEED_VALIDATION_MONGODB_URI")
        if not mongodb_uri:
            raise RuntimeError("SEED_VALIDATION_MONGODB_URI is not configured in the tool runtime.")
        store = _github_store()
        try:
            bundle = (
                store.read_project_seed_bundle(
                    branch=branch,
                    poc_id=poc_id,
                    code_version=code_version,
                    commit_sha=source_commit_sha,
                )
                if branch
                else store.read_seed_bundle(
                    poc_id=poc_id,
                    spec_version=spec_version,
                    code_version=code_version,
                    commit_sha=source_commit_sha,
                )
            )
            if branch:
                data_input = bundle.manifest["inputs"]["data_model"]
                query_input = bundle.manifest["inputs"]["query_patterns"]
                data_model = store.read_project_file(data_input["key"], data_input["commit_sha"])
                query = store.read_project_file(query_input["key"], query_input["commit_sha"])
                normalized_data, _ = normalize_data_model(json.loads(data_model.content))
                normalized_query, _ = normalize_query_patterns(json.loads(query.content))
                normalized_data, _ = apply_query_indexes(normalized_data, normalized_query)
                prefix = f"seed/{code_version}/"
            else:
                schema_path = f"pocs/{poc_id}/spec/{spec_version}/schema_design.json"
                query_path = f"pocs/{poc_id}/spec/{spec_version}/query_patterns.json"
                data_model = store.read_file(poc_id, schema_path, source_commit_sha)
                query = store.read_file(poc_id, query_path, source_commit_sha)
                normalized_data = json.loads(data_model.content)
                normalized_query = json.loads(query.content)
                prefix = f"pocs/{poc_id}/code/{code_version}/seed/"
            artifacts = {
                path.removeprefix(prefix): content
                for path, content in bundle.files.items()
                if path != f"{prefix}seed.manifest.json"
            }
            try:
                result = SeedValidatorClient.from_environment().validate_github_bundle(
                    poc_id=poc_id,
                    run_id=run_id,
                    task_id=task_id,
                    trace_id=trace_id,
                    spec_version=spec_version,
                    code_version=code_version,
                    repository=bundle.repository,
                    branch=bundle.branch,
                    source_commit_sha=source_commit_sha,
                    manifest_json=bundle.files[f"{prefix}seed.manifest.json"],
                    schema_design_json=data_model.content if not branch else "",
                    data_model_json=data_model.content if branch else "",
                    normalized_data_model_json=json.dumps(normalized_data, separators=(",", ":")) if branch else "",
                    query_patterns_json=query.content,
                    normalized_query_patterns_json=json.dumps(normalized_query, separators=(",", ":")) if branch else "",
                    artifacts=artifacts,
                    mongodb_uri=mongodb_uri,
                )
            except ValidatorClientError as error:
                result = error.payload
            report = {
                **result,
                "source_commit_sha": source_commit_sha,
                "correlation": {**_correlation(poc_id, run_id, task_id, trace_id), "producer": "seed-validator"},
            }
            report_key = f"{prefix}validation/{run_id}/report.json"
            report_files = {report_key: json.dumps(report, indent=2, sort_keys=True) + "\n"}
            committed = (
                store.commit_project_files_atomic(
                    branch,
                    report_files,
                    f"validation({poc_id}): record {code_version} run {run_id}",
                    expected_head_sha=source_commit_sha,
                )
                if branch
                else store.commit_files_atomic(
                    poc_id,
                    report_files,
                    f"validation({poc_id}): record {code_version} run {run_id}",
                    expected_head_sha=source_commit_sha,
                )
            )
        except GitHubStoreError as error:
            return _github_failure(error)
        return json.dumps(
            {
                **result,
                "source_commit_sha": source_commit_sha,
                "report_key": report_key,
                "report_commit_sha": committed.commit_sha,
            }
        )
