"""Agent state definition for poc-data-seed."""

from __future__ import annotations

from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from agent_poc_data_seed.observability import merge_timeline_events


class SeedWorkflowState(TypedDict):
    """Serializable control data for a self-validating seed run."""

    phase: Literal["generate", "validate", "repair", "repair_notes", "complete"]
    validation_attempts: int
    current_code_version: str
    source_commit_sha: NotRequired[str]
    seed_commit_sha: NotRequired[str]
    branch_head_sha: NotRequired[str]
    validation: NotRequired[dict[str, object]]
    final_result: NotRequired[dict[str, object]]


class ResolvedSeedContext(TypedDict):
    """Serializable graph-owned context resolved from shared state and GitHub."""

    pov_id: str
    repository: str
    branch: str
    branch_head_sha: str
    code_version: str
    data_model: dict[str, Any]
    defaults_applied: list[str]
    prior_seed: NotRequired[dict[str, str]]


class HelloWorldState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    invocation: NotRequired[dict[str, Any]]
    timeline: Annotated[list[dict[str, Any]], merge_timeline_events]
    workflow: NotRequired[SeedWorkflowState]
    resolved_context: NotRequired[ResolvedSeedContext]
    started_at: NotRequired[str]
    input_tokens: NotRequired[int]
    output_tokens: NotRequired[int]
    usage_available: NotRequired[bool]
