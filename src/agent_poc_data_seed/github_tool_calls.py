"""Normalize model-issued GitHub tool calls with workflow-owned arguments."""

from __future__ import annotations

from typing import Any, Mapping

from langchain_core.messages import AIMessage

from agent_poc_data_seed.envelope import SeedGenerationEnvelope
from agent_poc_data_seed.state import SeedWorkflowState


def normalize_github_tool_calls(
    response: AIMessage,
    envelope: SeedGenerationEnvelope,
    workflow: SeedWorkflowState,
    resolved_context: Mapping[str, Any] | None = None,
) -> AIMessage:
    """Replace model-supplied GitHub identity and version arguments."""
    if resolved_context and any(call["name"] == "read_seed_input_from_github_tool" for call in response.tool_calls):
        base_id = next(
            str(call.get("id", "seed-input"))
            for call in response.tool_calls
            if call["name"] == "read_seed_input_from_github_tool"
        )
        source = resolved_context["data_model"]
        read = {
            "name": "read_seed_input_from_github_tool",
            "id": f"{base_id}-data_model",
            "type": "tool_call",
            "args": {
                "artifact": "data_model",
                "poc_id": envelope.poc_id,
                "branch": resolved_context["branch"],
                "path": source["path"],
                "source_commit_sha": source["commit_sha"],
                "spec_version": envelope.spec_version or "",
            },
        }
        return response.model_copy(update={"tool_calls": [read]})
    normalized_calls: list[dict[str, Any]] = []
    previous_version = f"v{int(workflow['current_code_version'][1:]) - 1:03d}"
    validation = workflow.get("validation", {})
    validation_source = validation.get("source_commit_sha") if isinstance(validation, Mapping) else None
    repair_source = envelope.repair_source.source_commit_sha if envelope.repair_source else None
    for call in response.tool_calls:
        args = dict(call.get("args", {}))
        if call["name"] == "read_seed_input_from_github_tool":
            artifact_name = str(args.get("artifact", ""))
            if resolved_context:
                source = resolved_context["data_model"]
                args.update(
                    artifact="data_model",
                    poc_id=envelope.poc_id,
                    branch=resolved_context["branch"],
                    path=source["path"],
                    source_commit_sha=source["commit_sha"],
                )
            else:
                args.update(
                    poc_id=envelope.poc_id,
                    spec_version=envelope.spec_version,
                    source_commit_sha=envelope.spec_commit_sha,
                )
        elif call["name"] == "read_seed_repair_source_from_github_tool":
            args.update(
                poc_id=envelope.poc_id,
                spec_version=envelope.spec_version,
                previous_code_version=previous_version,
                source_commit_sha=validation_source or repair_source,
            )
            if resolved_context:
                args["branch"] = resolved_context["branch"]
        elif call["name"] == "commit_seed_bundle_to_github_tool":
            args.update(
                poc_id=envelope.poc_id,
                run_id=envelope.run_id,
                task_id=envelope.task_id,
                trace_id=envelope.trace_id,
                spec_version=envelope.spec_version,
                code_version=workflow["current_code_version"],
                spec_commit_sha=envelope.spec_commit_sha,
                expected_head_sha=workflow.get("branch_head_sha") or envelope.branch_head_sha,
            )
            if resolved_context:
                args.update(
                    branch=resolved_context["branch"],
                    spec_commit_sha="",
                    data_model_commit_sha=resolved_context["data_model"]["commit_sha"],
                    defaults_json=__import__("json").dumps(resolved_context.get("defaults_applied", [])),
                )
        normalized_calls.append({**call, "args": args})
    return response.model_copy(update={"tool_calls": normalized_calls})
