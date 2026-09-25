"""Stable orchestrator-facing AgentEnvelope response formatting."""

from __future__ import annotations

import json
from typing import Any, Mapping

from agent_poc_data_seed.envelope import SeedGenerationEnvelope
from agent_poc_data_seed.failures import public_error
from agent_poc_data_seed.observability import format_timeline


def format_terminal_response(
    envelope: SeedGenerationEnvelope,
    outcome: Mapping[str, Any],
) -> dict[str, Any]:
    """Format every final result as the stable AgentEnvelope response contract."""
    status = str(outcome.get("status", "failed"))
    validation = outcome.get("validation")
    validation = dict(validation) if isinstance(validation, Mapping) else {}
    raw_error = outcome.get("error")
    raw_error = dict(raw_error) if isinstance(raw_error, Mapping) else {}
    if not raw_error and isinstance(validation.get("error"), Mapping):
        raw_error = dict(validation["error"])
    outcome_code = outcome.get("code") or raw_error.get("code")
    artifacts: list[dict[str, str]] = []
    artifact_metadata = outcome.get("artifacts")
    if isinstance(artifact_metadata, list):
        for artifact in artifact_metadata:
            if isinstance(artifact, Mapping) and isinstance(artifact.get("key"), str):
                artifacts.append(
                    {
                        "kind": "code",
                        "key": artifact["key"],
                        "version": str(outcome.get("code_version", envelope.code_version)),
                    }
                )
    report_key = validation.get("report_key")
    if isinstance(report_key, str):
        artifacts.append(
            {
                "kind": "report",
                "key": report_key,
                "version": str(outcome.get("code_version", envelope.code_version)),
            }
        )
    result = {
        "poc_id": envelope.poc_id,
        "run_id": envelope.run_id,
        "spec_version": envelope.spec_version,
        "code_version": outcome.get("code_version", envelope.code_version),
        "target_database_name": outcome.get("target_database_name"),
        "validation": validation or None,
        "artifact_metadata": artifact_metadata if isinstance(artifact_metadata, list) else [],
        "source_commit_sha": outcome.get("source_commit_sha"),
        "report_commit_sha": outcome.get("report_commit_sha"),
        "timeline": format_timeline(outcome.get("timeline", []) if isinstance(outcome.get("timeline"), list) else []),
    }
    error = None
    if status != "succeeded":
        error = public_error(outcome_code, raw_error.get("message"))
        error["detail"]["validation_attempts"] = outcome.get("validation_attempts")
    usage = outcome.get("usage")
    usage = dict(usage) if isinstance(usage, Mapping) else {"input_tokens": 0, "output_tokens": 0, "duration_ms": 0}
    return {
        "response": {
            "task_id": envelope.task_id,
            "status": status,
            "result": result,
            "artifacts": artifacts,
            "error": error,
            "usage": usage,
        }
    }
def format_invalid_request_response(content: Any, error: ValueError) -> dict[str, Any]:
    """Return a stable failure wrapper when request validation fails before the graph starts."""
    task_id = "unknown"
    if isinstance(content, str):
        try:
            payload = json.loads(content)
            request = payload.get("request") if isinstance(payload, Mapping) else None
            if isinstance(request, Mapping) and isinstance(request.get("task_id"), str):
                task_id = request["task_id"]
        except json.JSONDecodeError:
            pass
    return {
        "response": {
            "task_id": task_id,
            "status": "failed",
            "result": {},
            "artifacts": [],
            "error": public_error("INVALID_REQUEST", error),
            "usage": {"input_tokens": 0, "output_tokens": 0, "duration_ms": 0},
        }
    }