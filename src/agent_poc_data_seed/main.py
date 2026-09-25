"""poc-data-seed Magenta app."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from agent_engine_sdk_langgraph import App

from agent_poc_data_seed.envelope import SeedGenerationEnvelope, fresh_seed_envelope, latest_seed_envelope
from agent_poc_data_seed.database_naming import target_database_name
from agent_poc_data_seed.failures import failure_definition
from agent_poc_data_seed.llm import build_llm
from agent_poc_data_seed.github_tool_calls import normalize_github_tool_calls
from agent_poc_data_seed.github_tools import GITHUB_TOOL_NAMES
from agent_poc_data_seed.github_storage import GitHubRepoStore, GitHubStoreError
from agent_poc_data_seed.limits import elapsed_milliseconds, extract_usage, limit_failure, parse_deadline
from agent_poc_data_seed.memory import save_episode, save_repair_pattern, successful_repair_pattern
from agent_poc_data_seed.observability import log_timeline_event, merge_timeline_events, timeline_event
from agent_poc_data_seed.response import format_invalid_request_response, format_terminal_response
from agent_poc_data_seed.artifact_layout import seed_key
from agent_poc_data_seed.state import HelloWorldState, SeedWorkflowState
from agent_poc_data_seed.shared_context import (
    apply_query_indexes,
    GitHubArtifactReference,
    next_seed_version,
    normalize_data_model,
    normalize_query_patterns,
)
from agent_poc_data_seed.shared_state import SharedPocContext, SharedStateError, load_shared_poc, publish_seed_pointer
from agent_poc_data_seed.system_message import LOCAL_SYSTEM_PROMPT, system_prompt_for
from agent_poc_data_seed.tools import register
from agent_poc_data_seed.workflow import decide_validation_only_result, decide_validation_result, next_code_version

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger(__name__)
load_dotenv()

app = App(app_name="poc-data-seed")

register(app)


@app.entrypoint
def build_agent() -> CompiledStateGraph:
    """Build the LangGraph agent."""
    logger.info("Building poc-data-seed graph")
    runtime_llm = app.llm(build_llm(temperature=0))
    tools = app.get_tools()
    tool_schemas = app.get_tool_schemas()

    def schema_name(schema: Any) -> str:
        if isinstance(schema, Mapping):
            function = schema.get("function")
            if isinstance(function, Mapping) and isinstance(function.get("name"), str):
                return function["name"]
            if isinstance(schema.get("name"), str):
                return schema["name"]
        name = getattr(schema, "name", None)
        if isinstance(name, str):
            return name
        return ""

    def envelope_for_state(state: HelloWorldState) -> SeedGenerationEnvelope | None:
        invocation = state.get("invocation")
        if invocation:
            return SeedGenerationEnvelope(**invocation)
        return latest_seed_envelope([message.content for message in state["messages"]])

    def llm_for_envelope(envelope: SeedGenerationEnvelope | None) -> Any:
        if envelope:
            if envelope.storage_mode == "github":
                allowed_tools = {"read_seed_input_from_github_tool", "commit_seed_bundle_to_github_tool"}
                if envelope.mode == "repair":
                    allowed_tools.add("read_seed_repair_source_from_github_tool")
                return runtime_llm.bind_tools(
                    [schema for schema in tool_schemas if schema_name(schema) in allowed_tools]
                )
            excluded_tools = set()
            excluded_tools.update(GITHUB_TOOL_NAMES)
            allowed_schemas = [
                schema
                for schema in tool_schemas
                if schema_name(schema) not in excluded_tools
            ]
            return runtime_llm.bind_tools(allowed_schemas)
        return runtime_llm.bind_tools(tool_schemas)

    def workflow_state(
        state: HelloWorldState, envelope: SeedGenerationEnvelope
    ) -> SeedWorkflowState:
        existing = state.get("workflow")
        if existing:
            return existing
        return {
            "phase": "validate" if envelope.validation_mode == "validate_existing" else "generate",
            "validation_attempts": 0,
            "current_code_version": envelope.code_version,
            **({"branch_head_sha": envelope.branch_head_sha} if envelope.branch_head_sha else {}),
        }

    def observed_tokens(state: HelloWorldState) -> int:
        return state.get("input_tokens", 0) + state.get("output_tokens", 0)

    def current_limit_failure(state: HelloWorldState, envelope: SeedGenerationEnvelope) -> str | None:
        return limit_failure(parse_deadline(envelope.deadline_at), envelope.max_tokens, observed_tokens(state))

    def usage_result(state: HelloWorldState) -> dict[str, int]:
        started_at = state.get("started_at") or datetime.now(timezone.utc).isoformat()
        return {
            "input_tokens": state.get("input_tokens", 0),
            "output_tokens": state.get("output_tokens", 0),
            "duration_ms": elapsed_milliseconds(started_at),
        }

    def limit_response(state: HelloWorldState, envelope: SeedGenerationEnvelope, code: str) -> dict[str, Any]:
        workflow = workflow_state(state, envelope)
        return format_terminal_response(
            envelope,
            {
                "status": "failed",
                "code": code,
                "code_version": workflow["current_code_version"],
                "usage": usage_result(state),
                "timeline": state.get("timeline", []),
            },
        )

    def observe(
        envelope: SeedGenerationEnvelope,
        event_type: str,
        discriminator: str,
        *,
        workflow: SeedWorkflowState,
        status: str,
        details: Mapping[str, Any] | None = None,
        validation_attempt: int | None = None,
    ) -> dict[str, Any]:
        event = timeline_event(
            event_type=event_type,
            discriminator=discriminator,
            poc_id=envelope.poc_id,
            run_id=envelope.run_id,
            task_id=envelope.task_id,
            trace_id=envelope.trace_id,
            code_version=workflow["current_code_version"],
            phase=workflow["phase"],
            validation_attempt=workflow["validation_attempts"] if validation_attempt is None else validation_attempt,
            status=status,
            details=details,
        )
        log_timeline_event(logger, event)
        return event

    def remember(
        envelope: SeedGenerationEnvelope,
        event: str,
        **details: Any,
    ) -> None:
        save_episode(
            app.memory,
            run_id=envelope.run_id,
            poc_id=envelope.poc_id,
            event=event,
            details=details,
        )

    def remember_successful_repair(
        workflow: SeedWorkflowState,
        envelope: SeedGenerationEnvelope,
    ) -> None:
        prior_validation = workflow.get("validation", {})
        external_failure = None
        previous_code_version = None
        if envelope.failure and envelope.repair_source:
            external_failure = {
                "failure_class": envelope.failure.failure_class,
                "attempt": envelope.failure.attempt,
            }
            previous_code_version = envelope.repair_source.previous_code_version
        pattern = successful_repair_pattern(
            current_version=workflow["current_code_version"],
            validation_attempts=workflow["validation_attempts"],
            prior_validation=prior_validation,
            external_failure=external_failure,
            previous_code_version=previous_code_version,
        )
        if not pattern:
            return
        save_repair_pattern(
            app.memory,
            run_id=envelope.run_id,
            poc_id=envelope.poc_id,
            failure_code=pattern.failure_code,
            failure_class=pattern.failure_class,
            source_version=pattern.source_version,
            repaired_version=workflow["current_code_version"],
            attempt=pattern.attempt,
            summary=repair_notes_content(workflow, envelope),
        )

    def workflow_context(workflow: SeedWorkflowState, envelope: SeedGenerationEnvelope) -> str:
        target_name = target_database_name(envelope.poc_id)
        if workflow["phase"] == "repair":
            validation = workflow.get("validation", {})
            error = validation.get("error", {}) if isinstance(validation, Mapping) else {}
            error = error if isinstance(error, Mapping) else {}
            code = str(error.get("code", "VALIDATION_FAILED"))
            message = re.sub(r"mongodb(?:\+srv)?://\S+", "[redacted MongoDB URI]", str(error.get("message", "")))[:500]
            github_context = (
                " The current GitHub branch head is "
                f"`{workflow['branch_head_sha']}`; use it as expected_head_sha instead of the original "
                "envelope branch head."
                if envelope.storage_mode == "github" and workflow.get("branch_head_sha")
                else ""
            )
            return (
                "\nRuntime workflow context: repair the current bundle at "
                f"code_version `{workflow['current_code_version']}`. Write all three artifacts and a new "
                "manifest; validation will run automatically after the manifest. "
                f"The actual seed database name is `{target_name}` unless DB_NAME overrides it. "
                f"Sanitized validator finding: {code}: {message}{github_context}"
            )
        return (
            "\nRuntime workflow context: generate the initial bundle at "
            f"code_version `{workflow['current_code_version']}`. Validation attempts remaining: "
            f"{envelope.max_validation_attempts - workflow['validation_attempts']}. "
            f"The actual seed database name is `{target_name}` unless DB_NAME overrides it."
        )

    def external_repair_context(envelope: SeedGenerationEnvelope) -> str:
        if not envelope.repair_source or not envelope.failure:
            raise ValueError("Repair workflow requires validated source and failure details")
        source = envelope.repair_source
        failure = envelope.failure
        return (
            "\nRuntime repair context: write the repaired bundle at "
            f"code_version `{envelope.code_version}`; do not overwrite `{source.previous_code_version}`. "
            "The runtime has already provided the verified prior source. "
            f"FailureReport: class={failure.failure_class}; exit_code={failure.exit_code}; "
            f"attempt={failure.attempt}/{failure.max_attempts}; finding={failure.stderr_excerpt}"
        )

    def recent_tool_messages(messages: list[Any]) -> list[ToolMessage]:
        results: list[ToolMessage] = []
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                break
            if isinstance(message, ToolMessage):
                results.append(message)
        return list(reversed(results))

    def parse_tool_result(message: ToolMessage) -> dict[str, Any]:
        try:
            result = json.loads(str(message.content))
        except json.JSONDecodeError:
            return {"status": "failed", "error": {"code": "VALIDATOR_INVALID_RESPONSE"}}
        return result if isinstance(result, dict) else {"status": "failed", "error": {"code": "VALIDATOR_INVALID_RESPONSE"}}

    def artifacts_for_version(
        messages: list[Any], envelope: SeedGenerationEnvelope, code_version: str, *, project_layout: bool = False
    ) -> list[dict[str, Any]]:
        prefix = f"seed/{code_version}/" if project_layout else seed_key(envelope.poc_id, code_version, "")
        artifacts: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, ToolMessage):
                continue
            result = parse_tool_result(message)
            nested_artifacts = result.get("artifacts")
            if isinstance(nested_artifacts, list):
                for artifact in nested_artifacts:
                    if isinstance(artifact, Mapping) and isinstance(artifact.get("key"), str) and artifact["key"].startswith(prefix):
                        artifacts.append({field: artifact[field] for field in ("key", "sha256", "bytes") if field in artifact})
            key = result.get("key") or result.get("path")
            if isinstance(key, str) and key.startswith(prefix):
                artifact = {"key": key}
                artifact.update({field: result[field] for field in ("sha256", "bytes") if field in result})
                artifacts.append(artifact)
        return artifacts

    def is_repair_bundle(workflow: SeedWorkflowState, envelope: SeedGenerationEnvelope) -> bool:
        return envelope.mode == "repair" or workflow["phase"] == "repair"

    def repair_notes_content(workflow: SeedWorkflowState, envelope: SeedGenerationEnvelope) -> str:
        validation = workflow.get("validation", {})
        error = validation.get("error", {}) if isinstance(validation, Mapping) else {}
        error = error if isinstance(error, Mapping) else {}
        if envelope.failure:
            source_version = envelope.repair_source.previous_code_version if envelope.repair_source else "unknown"
            failure_class = envelope.failure.failure_class
            failure_code = "EXTERNAL_FAILURE_REPORT"
            summary = envelope.failure.stderr_excerpt
        else:
            source_version = f"v{int(workflow['current_code_version'][1:]) - 1:03d}"
            failure_class = str(error.get("failure_class", "IMPLEMENTATION_FAILURE"))
            failure_code = str(error.get("code", "VALIDATION_FAILED"))
            summary = str(error.get("message", "Validation requested a repaired seed bundle."))
        summary = re.sub(r"mongodb(?:\+srv)?://\S+", "[redacted MongoDB URI]", summary).replace("\n", " ")[:500]
        return (
            "# Repair Notes\n\n"
            f"Source version: {source_version}\n"
            f"Repaired version: {workflow['current_code_version']}\n"
            f"Failure class: {failure_class}\n"
            f"Failure code: {failure_code}\n"
            "Changed files:\n"
            "- seed.js\n- package.json\n- SEED_README.md\n\n"
            f"Fix summary:\n{summary}\n"
        )

    def agent_node(state: HelloWorldState) -> dict:
        messages = state["messages"]
        system_prompt = LOCAL_SYSTEM_PROMPT
        try:
            envelope = envelope_for_state(state)
        except ValueError as error:
            invalid_content = messages[-1].content if messages else ""
            return {"messages": [AIMessage(content=json.dumps(format_invalid_request_response(invalid_content, error)))]}
        if envelope:
            limit_code = current_limit_failure(state, envelope)
            if limit_code:
                remember(
                    envelope,
                    "run_completed",
                    discriminator=limit_code,
                    to_phase="complete",
                    status="failed",
                    code_version=workflow_state(state, envelope)["current_code_version"],
                    validation_attempts=workflow_state(state, envelope)["validation_attempts"],
                    failure_code=limit_code,
                    failure_class="EXECUTION_LIMIT",
                )
                terminal = observe(
                    envelope,
                    "workflow_failed",
                    limit_code,
                    workflow=workflow_state(state, envelope),
                    status="failed",
                    details={"failure_code": limit_code, "failure_class": "EXECUTION_LIMIT"},
                )
                return {
                    "timeline": [terminal],
                    "messages": [AIMessage(content=json.dumps(limit_response({**state, "timeline": merge_timeline_events(state.get("timeline", []), [terminal])}, envelope, limit_code)))],
                }
            system_prompt = system_prompt_for(envelope.storage_mode, envelope.validation_mode, envelope.mode)
            if envelope.validation_mode == "generate_and_validate":
                current_workflow = workflow_state(state, envelope)
                system_prompt += workflow_context(current_workflow, envelope)
            else:
                current_workflow = None
            if envelope.mode == "repair" and (not current_workflow or current_workflow["phase"] != "repair"):
                system_prompt += external_repair_context(envelope)

        if not messages or not isinstance(messages[0], SystemMessage):
            prompt_messages = [SystemMessage(content=system_prompt)] + list(messages)
        else:
            prompt_messages = [SystemMessage(content=system_prompt)] + list(messages[1:])

        response = llm_for_envelope(envelope).invoke(prompt_messages)
        if envelope and envelope.storage_mode == "github" and response.tool_calls:
            response = normalize_github_tool_calls(
                response,
                envelope,
                workflow_state(state, envelope),
                state.get("resolved_context"),
            )
        input_tokens, output_tokens, usage_available = extract_usage(response)
        usage_update = {
            "input_tokens": state.get("input_tokens", 0) + input_tokens,
            "output_tokens": state.get("output_tokens", 0) + output_tokens,
            "usage_available": state.get("usage_available", False) or usage_available,
        }
        if envelope:
            updated_state = {**state, **usage_update}
            limit_code = current_limit_failure(updated_state, envelope)
            if limit_code:
                remember(
                    envelope,
                    "run_completed",
                    discriminator=limit_code,
                    to_phase="complete",
                    status="failed",
                    code_version=workflow_state(updated_state, envelope)["current_code_version"],
                    validation_attempts=workflow_state(updated_state, envelope)["validation_attempts"],
                    failure_code=limit_code,
                    failure_class="EXECUTION_LIMIT",
                )
                terminal = observe(
                    envelope,
                    "workflow_failed",
                    limit_code,
                    workflow=workflow_state(updated_state, envelope),
                    status="failed",
                    details={"failure_code": limit_code, "failure_class": "EXECUTION_LIMIT"},
                )
                updated_state = {
                    **updated_state,
                    "timeline": merge_timeline_events(state.get("timeline", []), [terminal]),
                }
                return {
                    **usage_update,
                    "timeline": [terminal],
                    "messages": [AIMessage(content=json.dumps(limit_response(updated_state, envelope, limit_code)))],
                }
        result: dict[str, Any] = {**usage_update, "messages": [response]}
        if envelope and envelope.validation_mode == "none" and not getattr(response, "tool_calls", None):
            manifest_names = {"commit_seed_bundle_to_github_tool"}
            if any(isinstance(message, ToolMessage) and message.name in manifest_names for message in messages):
                current = workflow_state(state, envelope)
                github_commits = [
                    parse_tool_result(message)
                    for message in messages
                    if isinstance(message, ToolMessage) and message.name == "commit_seed_bundle_to_github_tool"
                ]
                result["workflow"] = {
                    "phase": "complete",
                    "validation_attempts": current["validation_attempts"],
                    "current_code_version": current["current_code_version"],
                    "final_result": {
                        "status": "succeeded",
                        "poc_id": envelope.poc_id,
                        "run_id": envelope.run_id,
                        "code_version": current["current_code_version"],
                        "target_database_name": target_database_name(envelope.poc_id),
                        "artifacts": artifacts_for_version(
                            messages, envelope, current["current_code_version"], project_layout=bool(state.get("resolved_context"))
                        ),
                        "source_commit_sha": github_commits[-1].get("commit_sha") if github_commits else None,
                    },
                }
        return result

    def workflow_controller(state: HelloWorldState) -> dict:
        messages = state["messages"]
        envelope = envelope_for_state(state)
        if not envelope:
            return {}
        workflow = workflow_state(state, envelope)
        recent_tools = recent_tool_messages(messages)
        timeline_events: list[dict[str, Any]] = []
        for result in recent_tools:
            parsed = parse_tool_result(result)
            error = parsed.get("error", {}) if isinstance(parsed, Mapping) else {}
            error = error if isinstance(error, Mapping) else {}
            remember(
                envelope,
                "tool_completed",
                discriminator=result.tool_call_id,
                tool=result.name,
                tool_call_id=result.tool_call_id,
                status=parsed.get("status", "completed"),
                code_version=workflow["current_code_version"],
                validation_attempts=workflow["validation_attempts"],
                failure_code=error.get("code"),
                failure_class=error.get("failure_class"),
            )
            if result.name == "validate_github_seed_bundle_tool":
                detail = error.get("detail")
                detail = detail if isinstance(detail, Mapping) else {}
                result_attempt = workflow["validation_attempts"] + 1
                timeline_events.append(
                    observe(
                        envelope,
                        "validation_result",
                        result.tool_call_id,
                        workflow=workflow,
                        status=str(parsed.get("status", "failed")),
                        validation_attempt=result_attempt,
                        details={
                            "failure_code": error.get("code"),
                            "failure_class": error.get("failure_class") or detail.get("failure_class"),
                            "report_key": parsed.get("report_key"),
                            "source_commit_sha": parsed.get("source_commit_sha"),
                            "report_commit_sha": parsed.get("report_commit_sha"),
                        },
                    )
                )
            if result.name == "commit_seed_bundle_to_github_tool" and parsed.get("status") == "succeeded":
                nested_artifacts = parsed.get("artifacts")
                if isinstance(nested_artifacts, list):
                    for index, artifact in enumerate(nested_artifacts):
                        if not isinstance(artifact, Mapping):
                            continue
                        artifact_key = artifact.get("key")
                        event_type = "manifest_written" if str(artifact_key).endswith("seed.manifest.json") else "artifact_uploaded"
                        timeline_events.append(
                            observe(
                                envelope,
                                event_type,
                                f"{result.tool_call_id}-{index}",
                                workflow=workflow,
                                status="written",
                                details={
                                    "artifact_kind": str(artifact_key).rsplit("/", 1)[-1],
                                    "artifact_key": artifact_key,
                                    "sha256": artifact.get("sha256"),
                                    "bytes": artifact.get("bytes"),
                                    "source_commit_sha": parsed.get("commit_sha"),
                                },
                            )
                        )
                completion_type = "repair_completed" if is_repair_bundle(workflow, envelope) else "generation_completed"
                source_version = (
                    envelope.repair_source.previous_code_version
                    if envelope.repair_source
                    else f"v{int(workflow['current_code_version'][1:]) - 1:03d}" if completion_type == "repair_completed" else None
                )
                timeline_events.append(
                    observe(
                        envelope,
                        completion_type,
                        workflow["current_code_version"],
                        workflow=workflow,
                        status="succeeded",
                        details={
                            "source_code_version": source_version,
                            "new_code_version": workflow["current_code_version"] if completion_type == "repair_completed" else None,
                            "source_commit_sha": parsed.get("commit_sha"),
                        },
                    )
                )
        validation_results = [
            result
            for result in recent_tools
            if result.name == "validate_github_seed_bundle_tool"
        ]
        if envelope.validation_mode == "validate_existing":
            if not validation_results:
                return {}
            validation = parse_tool_result(validation_results[-1])
            decision = decide_validation_only_result(validation)
            validation_error = validation.get("error")
            validation_error = validation_error if isinstance(validation_error, Mapping) else {}
            timeline_events.append(
                observe(
                    envelope,
                    "validation_decision",
                    f"{envelope.code_version}-1-{decision.status}",
                    workflow=workflow,
                    status=decision.status,
                    validation_attempt=1,
                    details={
                        "decision": decision.status,
                        "failure_code": decision.code,
                        "failure_class": validation_error.get("failure_class"),
                        "max_validation_attempts": 1,
                    },
                )
            )
            final_result = {
                "status": decision.status,
                "code": decision.code,
                "poc_id": envelope.poc_id,
                "run_id": envelope.run_id,
                "code_version": envelope.code_version,
                "target_database_name": target_database_name(envelope.poc_id),
                "validation_attempts": 1,
                "validation": validation,
                "source_commit_sha": validation.get("source_commit_sha"),
                "report_commit_sha": validation.get("report_commit_sha"),
            }
            return {
                "timeline": timeline_events,
                "workflow": {
                    "phase": "complete",
                    "validation_attempts": 1,
                    "current_code_version": envelope.code_version,
                    "validation": validation,
                    "final_result": final_result,
                }
            }
        if envelope.validation_mode != "generate_and_validate":
            return {"timeline": timeline_events} if timeline_events else {}
        artifact_conflicts = [
            parse_tool_result(result)
            for result in recent_tools
            if parse_tool_result(result).get("error", {}).get("code") in {"ARTIFACT_EXISTS", "GITHUB_ARTIFACT_EXISTS"}
        ]
        if artifact_conflicts:
            return {
                "timeline": timeline_events,
                "workflow": {
                    "phase": "complete",
                    "validation_attempts": workflow["validation_attempts"],
                    "current_code_version": workflow["current_code_version"],
                    "final_result": {
                        "status": "failed",
                        "code": "CODE_VERSION_ALREADY_EXISTS",
                        "poc_id": envelope.poc_id,
                        "run_id": envelope.run_id,
                        "code_version": workflow["current_code_version"],
                        "error": artifact_conflicts[0]["error"],
                    },
                }
            }
        terminal_tool_failures = [
            parse_tool_result(result)
            for result in recent_tools
            if parse_tool_result(result).get("error", {}).get("code")
            in {
                "UNAUTHORIZED", "VALIDATION_TIMEOUT", "VALIDATION_CLEANUP_FAILED", "VALIDATOR_INFRASTRUCTURE_FAILURE", "VALIDATOR_INVALID_RESPONSE",
                "GITHUB_AUTH_FAILED", "GITHUB_NOT_FOUND", "GITHUB_CONFLICT", "GITHUB_RATE_LIMITED",
                "GITHUB_UNREACHABLE", "GITHUB_API_ERROR", "GITHUB_INVALID_RESPONSE",
                "GITHUB_CONTENT_MISMATCH", "GITHUB_MANIFEST_INVALID",
            }
        ]
        if terminal_tool_failures:
            failure = terminal_tool_failures[0]["error"]
            return {
                "timeline": timeline_events,
                "workflow": {
                    "phase": "complete",
                    "validation_attempts": workflow["validation_attempts"],
                    "current_code_version": workflow["current_code_version"],
                    "final_result": {
                        "status": "failed",
                        "code": failure["code"],
                        "poc_id": envelope.poc_id,
                        "run_id": envelope.run_id,
                        "code_version": workflow["current_code_version"],
                        "error": failure,
                    },
                }
            }
        if validation_results:
            validation = parse_tool_result(validation_results[-1])
            attempts = workflow["validation_attempts"] + 1
            decision = decide_validation_result(
                validation,
                validation_attempts=attempts,
                max_validation_attempts=envelope.max_validation_attempts,
            )
            validation_error = validation.get("error")
            validation_error = validation_error if isinstance(validation_error, Mapping) else {}
            timeline_events.append(
                observe(
                    envelope,
                    "validation_decision",
                    f"{workflow['current_code_version']}-{attempts}-{decision.action}",
                    workflow=workflow,
                    status=decision.action,
                    validation_attempt=attempts,
                    details={
                        "decision": decision.action,
                        "failure_code": validation_error.get("code"),
                        "failure_class": validation_error.get("failure_class"),
                        "max_validation_attempts": envelope.max_validation_attempts,
                    },
                )
            )
            if decision.action == "repair":
                limit_code = current_limit_failure(state, envelope)
                if limit_code:
                    remember(
                        envelope,
                        "repair_decision",
                        discriminator=f"limit-{attempts}",
                        decision="terminate",
                        status="failed",
                        code_version=workflow["current_code_version"],
                        validation_attempts=attempts,
                        failure_code=limit_code,
                        failure_class="EXECUTION_LIMIT",
                    )
                    return {
                        "timeline": timeline_events,
                        "workflow": {
                            "phase": "complete",
                            "validation_attempts": attempts,
                            "current_code_version": workflow["current_code_version"],
                            "final_result": {"status": "failed", "code": limit_code, "usage": usage_result(state)},
                        }
                    }
                next_version = next_code_version(workflow["current_code_version"])
                remember(
                    envelope,
                    "repair_decision",
                    discriminator=f"repair-{attempts}",
                    from_phase="validate",
                    to_phase="repair",
                    decision="repair",
                    status="running",
                    code_version=next_version,
                    validation_attempts=attempts,
                )
                timeline_events.append(
                    observe(
                        envelope,
                        "repair_requested",
                        f"{workflow['current_code_version']}-to-{next_version}",
                        workflow=workflow,
                        status="running",
                        details={
                            "source_code_version": workflow["current_code_version"],
                            "new_code_version": next_version,
                            "decision": "repair",
                            "failure_code": validation.get("error", {}).get("code") if isinstance(validation.get("error"), Mapping) else None,
                            "failure_class": validation.get("error", {}).get("failure_class") if isinstance(validation.get("error"), Mapping) else None,
                        },
                    )
                )
                return {
                    "timeline": timeline_events,
                    "workflow": {
                        "phase": "repair",
                        "validation_attempts": attempts,
                        "current_code_version": next_version,
                        **(
                            {"branch_head_sha": validation["report_commit_sha"]}
                            if isinstance(validation.get("report_commit_sha"), str)
                            else {}
                        ),
                        "validation": decision.result,
                    }
                }
            if decision.action == "succeeded":
                remember_successful_repair(workflow, envelope)
                final_result = {
                    "status": "succeeded",
                    "poc_id": envelope.poc_id,
                    "run_id": envelope.run_id,
                    "code_version": workflow["current_code_version"],
                    "target_database_name": target_database_name(envelope.poc_id),
                    "artifacts": artifacts_for_version(
                        messages, envelope, workflow["current_code_version"], project_layout=bool(state.get("resolved_context"))
                    ),
                    "validation": decision.result,
                    "source_commit_sha": workflow.get("seed_commit_sha") or decision.result.get("source_commit_sha"),
                    "seed_commit_sha": workflow.get("seed_commit_sha") or decision.result.get("source_commit_sha"),
                    "report_commit_sha": decision.result.get("report_commit_sha"),
                }
            elif decision.action == "blocked":
                final_result = {
                    "status": "blocked",
                    "code": "REQUEST_CONTRADICTION",
                    "poc_id": envelope.poc_id,
                    "run_id": envelope.run_id,
                    "target_database_name": target_database_name(envelope.poc_id),
                    "validation": decision.result,
                }
            elif decision.action == "exhausted":
                final_result = {
                    "status": "failed",
                    "code": "REPAIR_ATTEMPTS_EXHAUSTED",
                    "poc_id": envelope.poc_id,
                    "run_id": envelope.run_id,
                    "code_version": workflow["current_code_version"],
                    "target_database_name": target_database_name(envelope.poc_id),
                    "validation_attempts": attempts,
                    "validation": decision.result,
                }
            else:
                validation_error = decision.result.get("error")
                validation_error = validation_error if isinstance(validation_error, Mapping) else {}
                final_result = {
                    "status": "failed",
                    "code": validation_error.get("code", "VALIDATOR_INFRASTRUCTURE_FAILURE"),
                    "poc_id": envelope.poc_id,
                    "run_id": envelope.run_id,
                    "target_database_name": target_database_name(envelope.poc_id),
                    "validation": decision.result,
                }
            remember(
                envelope,
                "validation_decision",
                discriminator=f"{decision.action}-{attempts}",
                from_phase="validate",
                to_phase="complete",
                decision=decision.action,
                status=final_result["status"],
                code_version=workflow["current_code_version"],
                validation_attempts=attempts,
                failure_code=final_result.get("code"),
            )
            return {
                "timeline": timeline_events,
                "workflow": {
                    "phase": "complete",
                    "validation_attempts": attempts,
                    "current_code_version": workflow["current_code_version"],
                    "validation": decision.result,
                    "final_result": final_result,
                }
            }

        github_commits = [
            parse_tool_result(result)
            for result in recent_tools
            if result.name == "commit_seed_bundle_to_github_tool"
            and parse_tool_result(result).get("status") == "succeeded"
        ]
        if github_commits:
            commit_sha = github_commits[-1].get("commit_sha")
            if not isinstance(commit_sha, str):
                return {
                    "timeline": timeline_events,
                    "workflow": {
                        "phase": "complete",
                        "validation_attempts": workflow["validation_attempts"],
                        "current_code_version": workflow["current_code_version"],
                        "final_result": {"status": "failed", "code": "VALIDATOR_INVALID_RESPONSE"},
                    },
                }
            return {
                "timeline": timeline_events,
                "workflow": {
                    "phase": "validate",
                    "validation_attempts": workflow["validation_attempts"],
                    "current_code_version": workflow["current_code_version"],
                    "source_commit_sha": commit_sha,
                    "seed_commit_sha": commit_sha,
                    "branch_head_sha": commit_sha,
                    "validation": workflow.get("validation", {}),
                },
            }

        return {"timeline": timeline_events} if timeline_events else {}

    def request_validation(state: HelloWorldState) -> dict:
        messages = state["messages"]
        envelope = envelope_for_state(state)
        if not envelope:
            raise ValueError("Self-validating workflow requires an AgentEnvelope")
        workflow = workflow_state(state, envelope)
        started = observe(
            envelope,
            "validation_started",
            f"{workflow['current_code_version']}-{workflow['validation_attempts'] + 1}",
            workflow=workflow,
            status="running",
            details={"max_validation_attempts": envelope.max_validation_attempts},
            validation_attempt=workflow["validation_attempts"] + 1,
        )
        args = {
            "poc_id": envelope.poc_id,
            "run_id": envelope.run_id,
            "task_id": envelope.task_id,
            "trace_id": envelope.trace_id,
            "spec_version": envelope.spec_version,
            "code_version": workflow["current_code_version"],
        }
        resolved_context = state.get("resolved_context")
        if resolved_context:
            args["branch"] = resolved_context["branch"]
        source_commit_sha = workflow.get("source_commit_sha") or envelope.source_commit_sha
        if not source_commit_sha:
            raise ValueError("GitHub validation requires a committed source SHA")
        args["source_commit_sha"] = source_commit_sha
        return {
            "timeline": [started],
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "validate_github_seed_bundle_tool",
                            "args": args,
                            "id": f"seed-validation-{workflow['validation_attempts'] + 1}",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }

    def request_repair_source(state: HelloWorldState) -> dict:
        envelope = envelope_for_state(state)
        if not envelope or not envelope.repair_source:
            raise ValueError("External repair requires immutable prior-source metadata")
        source = envelope.repair_source
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_seed_repair_source_from_github_tool",
                            "args": {
                                "poc_id": envelope.poc_id,
                                "spec_version": envelope.spec_version,
                                "previous_code_version": source.previous_code_version,
                                "source_commit_sha": source.source_commit_sha,
                            },
                            "id": "seed-repair-source",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }

    def final_result_node(state: HelloWorldState) -> dict:
        workflow = state.get("workflow")
        if not workflow or "final_result" not in workflow:
            raise ValueError("Self-validating workflow ended without a terminal result")
        envelope = envelope_for_state(state)
        if not envelope:
            raise ValueError("Terminal seed result requires an AgentEnvelope")
        final_result = dict(workflow["final_result"])
        final_result.setdefault("code_version", workflow["current_code_version"])
        status = str(final_result.get("status", "failed"))
        resolved = state.get("resolved_context")
        if status == "succeeded" and resolved:
            try:
                seed_commit_sha = str(final_result.get("seed_commit_sha") or final_result.get("source_commit_sha") or "")
                report_commit_sha = str(final_result.get("report_commit_sha") or "")
                code_version = workflow["current_code_version"]
                store = GitHubRepoStore.from_environment()
                store.read_project_seed_bundle(
                    branch=resolved["branch"],
                    poc_id=envelope.poc_id,
                    code_version=code_version,
                    commit_sha=seed_commit_sha,
                )
                report_path = f"seed/{code_version}/validation/{envelope.run_id}/report.json"
                report_artifact = store.read_project_file(report_path, report_commit_sha)
                report = json.loads(report_artifact.content)
                if report.get("status") != "succeeded" or report.get("source_commit_sha") != seed_commit_sha:
                    raise ValueError("Validation report does not identify the successful seed bundle")
                data_reference = GitHubArtifactReference(
                    resolved["repository"], resolved["branch"], resolved["data_model"]["path"],
                    resolved["data_model"]["commit_sha"], resolved["data_model"]["url"],
                )
                query_reference = GitHubArtifactReference(
                    resolved["repository"], resolved["branch"], resolved["query_patterns"]["path"],
                    resolved["query_patterns"]["commit_sha"], resolved["query_patterns"]["url"],
                )
                shared = SharedPocContext(
                    envelope.poc_id,
                    data_reference,
                    query_reference,
                    resolved.get("prior_seed"),
                )
                seed_path = f"seed/{code_version}/seed.js"
                publish_seed_pointer(shared, {
                    "path": seed_path,
                    "commit_sha": seed_commit_sha,
                    "url": f"https://github.com/{resolved['repository']}/blob/{resolved['branch']}/{seed_path}",
                })
                final_result["seed_commit_sha"] = seed_commit_sha
            except SharedStateError as error:
                status = "failed"
                final_result = {
                    **final_result,
                    "status": "failed",
                    "code": error.code,
                    "error": {"code": error.code, "message": str(error)},
                }
            except (GitHubStoreError, ValueError, json.JSONDecodeError) as error:
                status = "failed"
                code = error.code if isinstance(error, GitHubStoreError) else "GITHUB_CONTENT_MISMATCH"
                final_result = {
                    **final_result,
                    "status": "failed",
                    "code": code,
                    "error": {"code": code, "message": str(error)},
                }
        event_type = "workflow_completed" if status == "succeeded" else "workflow_failed"
        failure_code = final_result.get("code")
        terminal = observe(
            envelope,
            event_type,
            str(final_result.get("code", status)),
            workflow=workflow,
            status=status,
            details={
                "failure_code": failure_code,
                "failure_class": failure_definition(failure_code).failure_class if failure_code else None,
                "final_code_version": workflow["current_code_version"],
                "total_attempts": workflow["validation_attempts"],
                "duration_ms": usage_result(state)["duration_ms"],
            },
        )
        timeline = merge_timeline_events(state.get("timeline", []), [terminal])
        outcome = {**final_result, "usage": usage_result(state), "timeline": timeline}
        remember(
            envelope,
            "run_completed",
            discriminator=str(outcome.get("status", "unknown")),
            from_phase=workflow["phase"],
            to_phase="complete",
            status=outcome.get("status"),
            code_version=workflow["current_code_version"],
            validation_attempts=workflow["validation_attempts"],
            failure_code=outcome.get("code"),
        )
        response = format_terminal_response(envelope, outcome)
        return {"timeline": [terminal], "messages": [AIMessage(content=json.dumps(response))]}

    def invalid_request_node(state: HelloWorldState) -> dict:
        content = state["messages"][-1].content if state["messages"] else ""
        try:
            latest_seed_envelope([message.content for message in state["messages"]])
        except ValueError as error:
            return {"messages": [AIMessage(content=json.dumps(format_invalid_request_response(content, error)))]}
        raise ValueError("Invalid request node reached without an envelope validation error")

    def initialize_node(state: HelloWorldState) -> dict:
        try:
            content = state["messages"][-1].content if state["messages"] else ""
            envelope = fresh_seed_envelope(content) if isinstance(content, str) else None
        except ValueError:
            envelope = None
        if envelope:
            workflow = workflow_state(state, envelope)
            remember(
                envelope,
                "run_started",
                discriminator=envelope.task_id,
                to_phase="generate",
                status="running",
                code_version=envelope.code_version,
                validation_attempts=0,
            )
            timeline = []
        else:
            timeline = []
        return {
            **({"invocation": asdict(envelope)} if envelope else {}),
            "started_at": state.get("started_at") or datetime.now(timezone.utc).isoformat(),
            "input_tokens": state.get("input_tokens", 0),
            "output_tokens": state.get("output_tokens", 0),
            "usage_available": state.get("usage_available", False),
            "timeline": timeline,
        }

    def load_shared_context_node(state: HelloWorldState) -> dict:
        envelope = envelope_for_state(state)
        if not envelope:
            return {}
        try:
            shared = load_shared_poc(envelope.poc_id)
            store = GitHubRepoStore.from_environment()
            branch_info = store.resolve_existing_branch(shared.branch)
            data_model_artifact = store.read_project_file(shared.data_model.path, shared.data_model.commit_sha)
            query_artifact = store.read_project_file(shared.query_patterns.path, shared.query_patterns.commit_sha)
            data_model_raw = json.loads(data_model_artifact.content)
            query_patterns_raw = json.loads(query_artifact.content)
            if not isinstance(data_model_raw, Mapping) or not isinstance(query_patterns_raw, Mapping):
                raise ValueError("POC specification artifacts must contain JSON objects")
            data_model, data_defaults = normalize_data_model(data_model_raw)
            query_patterns, query_defaults = normalize_query_patterns(query_patterns_raw)
            collection_names = {item["name"] for item in data_model["collections"]}
            unknown = sorted({item["collection"] for item in query_patterns["patterns"]} - collection_names)
            if unknown:
                raise ValueError(f"Query patterns reference unknown collections: {', '.join(unknown)}")
            data_model, index_defaults = apply_query_indexes(data_model, query_patterns)
            paths = store.list_project_paths(branch_info["head_sha"])
            code_version = next_seed_version(list(paths))
        except SharedStateError as error:
            code, message = error.code, str(error)
        except GitHubStoreError as error:
            code, message = error.code, str(error)
        except (ValueError, json.JSONDecodeError) as error:
            code, message = "REQUEST_CONTRADICTION", str(error)
        else:
            return {
                "resolved_context": {
                    "pov_id": shared.pov_id,
                    "repository": shared.data_model.repository,
                    "branch": shared.branch,
                    "branch_head_sha": branch_info["head_sha"],
                    "code_version": code_version,
                    "data_model": {
                        "path": shared.data_model.path,
                        "commit_sha": shared.data_model.commit_sha,
                        "url": shared.data_model.url,
                        "sha256": data_model_artifact.sha256,
                        "bytes": data_model_artifact.bytes,
                        "content": data_model,
                    },
                    "query_patterns": {
                        "path": shared.query_patterns.path,
                        "commit_sha": shared.query_patterns.commit_sha,
                        "url": shared.query_patterns.url,
                        "sha256": query_artifact.sha256,
                        "bytes": query_artifact.bytes,
                        "content": query_patterns,
                    },
                    "defaults_applied": [*data_defaults, *query_defaults, *index_defaults],
                    **({"prior_seed": shared.prior_seed} if shared.prior_seed is not None else {}),
                },
                "workflow": {
                    "phase": "generate",
                    "validation_attempts": 0,
                    "current_code_version": code_version,
                    "branch_head_sha": branch_info["head_sha"],
                },
                "timeline": [
                    observe(
                        envelope,
                        "run_started",
                        envelope.task_id,
                        workflow={
                            "phase": "generate",
                            "validation_attempts": 0,
                            "current_code_version": code_version,
                            "branch_head_sha": branch_info["head_sha"],
                        },
                        status="running",
                        details={"max_validation_attempts": envelope.max_validation_attempts},
                    ),
                    observe(
                        envelope,
                        "generation_started",
                        code_version,
                        workflow={
                            "phase": "generate",
                            "validation_attempts": 0,
                            "current_code_version": code_version,
                            "branch_head_sha": branch_info["head_sha"],
                        },
                        status="running",
                    ),
                ],
            }
        workflow = workflow_state(state, envelope)
        return {
            "workflow": {
                **workflow,
                "phase": "complete",
                "final_result": {
                    "status": "failed",
                    "code": code,
                    "error": {"code": code, "message": message},
                },
            }
        }

    def limit_failure_node(state: HelloWorldState) -> dict:
        envelope = envelope_for_state(state)
        if not envelope:
            raise ValueError("Limit failure requires an AgentEnvelope")
        code = current_limit_failure(state, envelope)
        if not code:
            raise ValueError("Limit failure node reached without an exceeded limit")
        remember(
            envelope,
            "run_completed",
            discriminator=code,
            to_phase="complete",
            status="failed",
            code_version=workflow_state(state, envelope)["current_code_version"],
            validation_attempts=workflow_state(state, envelope)["validation_attempts"],
            failure_code=code,
            failure_class="EXECUTION_LIMIT",
        )
        terminal = observe(
            envelope,
            "workflow_failed",
            code,
            workflow=workflow_state(state, envelope),
            status="failed",
            details={"failure_code": code, "failure_class": "EXECUTION_LIMIT"},
        )
        updated_state = {**state, "timeline": merge_timeline_events(state.get("timeline", []), [terminal])}
        return {"timeline": [terminal], "messages": [AIMessage(content=json.dumps(limit_response(updated_state, envelope, code)))]}

    def should_continue(state: HelloWorldState) -> Literal["tools", "final", "end"]:
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:  # type: ignore[union-attr]
            return "tools"
        workflow = state.get("workflow")
        return "final" if workflow and workflow["phase"] == "complete" else "end"

    def route_after_tools(state: HelloWorldState) -> Literal["agent", "validation", "final"]:
        workflow = state.get("workflow")
        if not workflow:
            return "agent"
        if workflow["phase"] == "complete":
            return "final"
        if workflow["phase"] == "validate":
            return "validation"
        return "agent"

    def route_start(state: HelloWorldState) -> Literal["load_shared_context", "invalid_request", "limit_failure"]:
        try:
            envelope = envelope_for_state(state)
        except ValueError:
            return "invalid_request"
        if envelope and current_limit_failure(state, envelope):
            return "limit_failure"
        return "load_shared_context"

    def route_after_shared_context(state: HelloWorldState) -> Literal["agent", "final"]:
        workflow = state.get("workflow")
        return "final" if workflow and workflow["phase"] == "complete" else "agent"

    builder = StateGraph(HelloWorldState)
    builder.add_node("initialize", initialize_node)
    builder.add_node("load_shared_context", load_shared_context_node)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", ToolNode(tools))
    builder.add_node("workflow_controller", workflow_controller)
    builder.add_node("validation", request_validation)
    builder.add_node("repair_source", request_repair_source)
    builder.add_node("final", final_result_node)
    builder.add_node("invalid_request", invalid_request_node)
    builder.add_node("limit_failure", limit_failure_node)
    builder.add_conditional_edges(
        "initialize",
        route_start,
        {"load_shared_context": "load_shared_context", "invalid_request": "invalid_request", "limit_failure": "limit_failure"},
    )
    builder.add_edge(START, "initialize")
    builder.add_conditional_edges("load_shared_context", route_after_shared_context, {"agent": "agent", "final": "final"})
    builder.add_conditional_edges("agent", should_continue, {"tools": "tools", "final": "final", "end": END})
    builder.add_edge("tools", "workflow_controller")
    builder.add_conditional_edges(
        "workflow_controller",
        route_after_tools,
        {"agent": "agent", "validation": "validation", "final": "final"},
    )
    builder.add_edge("validation", "tools")
    builder.add_edge("repair_source", "tools")
    builder.add_edge("final", END)
    builder.add_edge("invalid_request", END)
    builder.add_edge("limit_failure", END)
    return builder.compile(checkpointer=app.checkpointer())


def main() -> None:
    """Run the hello-world agent."""
    app.run()


if __name__ == "__main__":
    main()
