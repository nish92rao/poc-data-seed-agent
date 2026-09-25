"""Deterministic routing policy for self-validating seed workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from agent_poc_data_seed.failures import failure_definition

WorkflowDecision = Literal["succeeded", "repair", "blocked", "exhausted", "infrastructure_error"]
ValidationOnlyDecision = Literal["succeeded", "blocked", "failed"]

_BLOCKED_FAILURE_CLASSES = frozenset(
    {"CONTRACT_MISMATCH", "REQUEST_CONTRADICTION", "SCHEMA_CONSTRAINT"}
)
_INFRASTRUCTURE_ERROR_CODES = frozenset(
    {"UNAUTHORIZED", "VALIDATION_TIMEOUT", "VALIDATION_CLEANUP_FAILED", "VALIDATOR_UNREACHABLE", "VALIDATOR_INVALID_RESPONSE"}
)


@dataclass(frozen=True)
class ValidationDecision:
    """The next deterministic workflow action for one validator result."""

    action: WorkflowDecision
    result: dict[str, Any]


@dataclass(frozen=True)
class ValidationOnlyResult:
    """Terminal status and code for validation of an existing bundle."""

    status: ValidationOnlyDecision
    code: str | None


def decide_validation_result(
    result: Mapping[str, Any],
    *,
    validation_attempts: int,
    max_validation_attempts: int,
) -> ValidationDecision:
    """Route a structured validator result without asking the model to count retries."""
    sanitized_result = dict(result)
    if sanitized_result.get("status") == "succeeded":
        return ValidationDecision("succeeded", sanitized_result)

    error = sanitized_result.get("error")
    if not isinstance(error, Mapping):
        return ValidationDecision("infrastructure_error", sanitized_result)

    code = error.get("code")
    failure_class = error.get("failure_class")
    if code in _INFRASTRUCTURE_ERROR_CODES:
        return ValidationDecision("infrastructure_error", sanitized_result)
    if failure_class in _BLOCKED_FAILURE_CLASSES:
        return ValidationDecision("blocked", sanitized_result)
    if not failure_definition(code).repairable or failure_class not in {None, "IMPLEMENTATION_FAILURE"}:
        return ValidationDecision("infrastructure_error", sanitized_result)
    if validation_attempts >= max_validation_attempts:
        return ValidationDecision("exhausted", sanitized_result)
    return ValidationDecision("repair", sanitized_result)


def decide_validation_only_result(result: Mapping[str, Any]) -> ValidationOnlyResult:
    """Classify an existing-bundle validation without invoking repair."""
    if result.get("status") == "succeeded":
        return ValidationOnlyResult("succeeded", None)
    error = result.get("error")
    if not isinstance(error, Mapping):
        return ValidationOnlyResult("failed", "VALIDATOR_INVALID_RESPONSE")
    if error.get("failure_class") in _BLOCKED_FAILURE_CLASSES:
        return ValidationOnlyResult("blocked", "REQUEST_CONTRADICTION")
    code = str(error.get("code") or "VALIDATOR_INFRASTRUCTURE_FAILURE")
    return ValidationOnlyResult("failed", code)


def next_code_version(code_version: str) -> str:
    """Return the next fixed-width seed code version."""
    if len(code_version) != 4 or not code_version.startswith("v") or not code_version[1:].isdigit():
        raise ValueError("code_version must use the vNNN format for autonomous repair")
    current = int(code_version[1:])
    if current >= 999:
        raise ValueError("Seed version space is exhausted")
    return f"v{current + 1:03d}"