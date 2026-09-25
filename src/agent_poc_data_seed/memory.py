"""Safe, fail-open harness memory writes for seed workflows."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping

from agent_poc_data_seed.failures import sanitize_message

logger = logging.getLogger(__name__)

_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class RepairPattern:
    failure_code: str
    failure_class: str
    source_version: str
    attempt: int


def event_id(run_id: str, event: str, discriminator: str = "") -> str:
    """Build a deterministic identifier so replayed graph events are recognizable."""
    parts = (run_id, event, discriminator)
    return "__".join(_SAFE_ID.sub("_", part).strip("_") for part in parts if part)


def procedure_name(failure_code: str, failure_class: str) -> str:
    """Build the memory API's required kebab-case procedure identifier."""
    value = f"poc-data-seed-repair-{failure_code}-{failure_class}".lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    if len(normalized) <= 64:
        return normalized
    digest = hashlib.sha256(normalized.encode("ascii")).hexdigest()[:8]
    return f"{normalized[:55].rstrip('-')}-{digest}"


def successful_repair_pattern(
    *,
    current_version: str,
    validation_attempts: int,
    prior_validation: Mapping[str, Any] | None = None,
    external_failure: Mapping[str, Any] | None = None,
    previous_code_version: str | None = None,
) -> RepairPattern | None:
    """Derive safe repair provenance only when a repaired version succeeded."""
    if external_failure and previous_code_version:
        return RepairPattern(
            "EXTERNAL_FAILURE_REPORT",
            str(external_failure.get("failure_class", "IMPLEMENTATION_FAILURE")),
            previous_code_version,
            max(1, int(external_failure.get("attempt", 1))),
        )
    error = prior_validation.get("error") if isinstance(prior_validation, Mapping) else None
    if not isinstance(error, Mapping) or not error:
        return None
    return RepairPattern(
        str(error.get("code", "VALIDATION_FAILED")),
        str(error.get("failure_class", "IMPLEMENTATION_FAILURE")),
        f"v{int(current_version[1:]) - 1:03d}",
        validation_attempts + 1,
    )


def save_episode(memory: Any, *, run_id: str, poc_id: str, event: str, details: Mapping[str, Any]) -> bool:
    """Write one sanitized run episode without affecting workflow execution."""
    safe_details = _safe_details(details)
    identifier = event_id(run_id, event, str(safe_details.get("discriminator", "")))
    content = {
        "event_id": identifier,
        "run_id": run_id,
        "poc_id": poc_id,
        "event": event,
        **safe_details,
    }
    try:
        result = memory.save_episode(
            title=f"Seed run {run_id}: {event}",
            content=json.dumps(content, sort_keys=True, separators=(",", ":")),
            summary=f"Seed workflow event {event} for run {run_id}",
            participants=["poc-data-seed"],
            tags=["poc-data-seed", "seed-run", f"run:{run_id}"],
            metadata={"event_id": identifier, "run_id": run_id, "poc_id": poc_id, "event": event},
        )
        acknowledged = _acknowledged(result)
        if not acknowledged:
            logger.warning("Harness episodic memory write was not acknowledged")
        return acknowledged
    except Exception as error:  # Memory must never control seed execution.
        logger.warning("Harness episodic memory write failed: %s", sanitize_message(error))
        return False


def save_repair_pattern(
    memory: Any,
    *,
    run_id: str,
    poc_id: str,
    failure_code: str,
    failure_class: str,
    source_version: str,
    repaired_version: str,
    attempt: int,
    summary: str,
) -> bool:
    """Store a successful repair playbook as native procedural memory."""
    safe_code = _safe_token(failure_code, "VALIDATION_FAILED")
    safe_class = _safe_token(failure_class, "IMPLEMENTATION_FAILURE")
    signature = f"{safe_code}::{safe_class}"
    identifier = procedure_name(safe_code, safe_class)
    content = {
        "failure_signature": signature,
        "source_code_version": _safe_token(source_version, "unknown"),
        "repaired_code_version": _safe_token(repaired_version, "unknown"),
        "repair_attempt": max(1, int(attempt)),
        "changed_artifacts": ["seed.js", "package.json", "SEED_README.md", "REPAIR_NOTES.md"],
        "fix_summary": sanitize_message(summary)[:500],
    }
    try:
        result = memory.save_procedure(
            procedure=identifier,
            description=f"Repair seed failure {signature}",
            content=json.dumps(content, sort_keys=True, separators=(",", ":")),
            steps=[
                {
                    "step_type": "instruction",
                    "content": "Review the sanitized validator finding and immutable source bundle.",
                    "description": "Identify the bounded implementation defect.",
                },
                {
                    "step_type": "instruction",
                    "content": "Change only the seed artifacts required to address the finding.",
                    "description": "Preserve the approved schema and query contract.",
                },
                {
                    "step_type": "validation",
                    "content": "Write repair notes and validate the new immutable code version.",
                    "description": "Accept the repair only after validator success.",
                },
            ],
            allowed_tools=[
                "read_seed_repair_source_from_github_tool",
                "commit_seed_bundle_to_github_tool",
                "validate_github_seed_bundle_tool",
            ],
            tags=["poc-data-seed", "repair-pattern", safe_code, safe_class],
            metadata={
                "failure_signature": signature,
                "run_id": run_id,
                "poc_id": poc_id,
            },
            update_existing=True,
        )
        acknowledged = _acknowledged(result)
        if not acknowledged:
            logger.warning("Harness procedural memory write was not acknowledged")
        return acknowledged
    except Exception as error:  # Memory must never control seed execution.
        logger.warning("Harness procedural memory write failed: %s", sanitize_message(error))
        return False


def _safe_details(details: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "discriminator",
        "tool",
        "tool_call_id",
        "from_phase",
        "to_phase",
        "decision",
        "status",
        "code_version",
        "validation_attempts",
        "failure_code",
        "failure_class",
    }
    safe: dict[str, Any] = {}
    for key in allowed:
        value = details.get(key)
        if isinstance(value, bool) or value is None:
            safe[key] = value
        elif isinstance(value, int):
            safe[key] = value
        elif isinstance(value, str):
            safe[key] = sanitize_message(value)[:200]
    return safe


def _safe_token(value: str, fallback: str) -> str:
    normalized = _SAFE_ID.sub("_", str(value)).strip("_")[:100]
    return normalized or fallback


def _acknowledged(result: Any) -> bool:
    acknowledged = getattr(result, "acknowledged", result)
    return bool(acknowledged)