"""Stable public failure taxonomy for the data seeding agent."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FailureDefinition:
    """Public behavior for one stable failure code."""

    failure_class: str
    message: str
    retryable: bool
    repairable: bool = False
    preserve_safe_message: bool = False


FAILURES: dict[str, FailureDefinition] = {
    "INVALID_REQUEST": FailureDefinition("REQUEST_VALIDATION_FAILURE", "The seed request is invalid.", False, preserve_safe_message=True),
    "POC_NOT_FOUND": FailureDefinition("REQUEST_VALIDATION_FAILURE", "No POC was found for the supplied poc_id.", False),
    "SHARED_STATE_UNAVAILABLE": FailureDefinition("PLATFORM_INFRASTRUCTURE_FAILURE", "Shared POC state is temporarily unavailable.", True),
    "SHARED_STATE_INVALID": FailureDefinition("REQUEST_VALIDATION_FAILURE", "Shared POC state is invalid.", False, preserve_safe_message=True),
    "SHARED_STATE_CONFLICT": FailureDefinition("ARTIFACT_CONFLICT", "POC state changed before the seed result could be published.", True),
    "REQUEST_CONTRADICTION": FailureDefinition("REQUEST_CONTRADICTION", "The seed requirements are contradictory.", False, preserve_safe_message=True),
    "VALIDATION_FAILED": FailureDefinition("IMPLEMENTATION_FAILURE", "The generated seed bundle failed validation.", False, repairable=True, preserve_safe_message=True),
    "REPAIR_ATTEMPTS_EXHAUSTED": FailureDefinition("IMPLEMENTATION_FAILURE", "The seed bundle still failed validation after all repair attempts.", False),
    "CODE_VERSION_ALREADY_EXISTS": FailureDefinition("ARTIFACT_CONFLICT", "The requested code version already exists.", False),
    "ARTIFACT_EXISTS": FailureDefinition("ARTIFACT_CONFLICT", "The requested artifact already exists.", False),
    "VALIDATION_TIMEOUT": FailureDefinition("VALIDATOR_INFRASTRUCTURE_FAILURE", "Seed validation timed out.", True),
    "VALIDATION_CLEANUP_FAILED": FailureDefinition("VALIDATOR_INFRASTRUCTURE_FAILURE", "Validation database cleanup failed.", True),
    "VALIDATOR_INFRASTRUCTURE_FAILURE": FailureDefinition("VALIDATOR_INFRASTRUCTURE_FAILURE", "The seed validator is temporarily unavailable.", True),
    "UNAUTHORIZED": FailureDefinition("AUTHORIZATION_FAILURE", "The validator request was not authorized.", False),
    "VALIDATOR_INVALID_RESPONSE": FailureDefinition("VALIDATOR_PROTOCOL_FAILURE", "The validator returned an invalid response.", True),
    "ARTIFACT_MALFORMED": FailureDefinition("IMPLEMENTATION_FAILURE", "The generated artifact bundle is malformed.", False, repairable=True),
    "PACKAGE_JSON_INVALID": FailureDefinition("IMPLEMENTATION_FAILURE", "The generated package.json is invalid.", False, repairable=True),
    "SEED_SCRIPT_INVALID": FailureDefinition(
        "IMPLEMENTATION_FAILURE",
        "The generated seed script is invalid.",
        False,
        repairable=True,
        preserve_safe_message=True,
    ),
    "ARTIFACT_SIZE_EXCEEDED": FailureDefinition("IMPLEMENTATION_FAILURE", "A generated artifact exceeds its size limit.", False, repairable=True),
    "ARTIFACT_HASH_MISMATCH": FailureDefinition("ARTIFACT_INTEGRITY_FAILURE", "Artifact content does not match its manifest.", False),
    "ARTIFACT_SIZE_MISMATCH": FailureDefinition("ARTIFACT_INTEGRITY_FAILURE", "Artifact size does not match its manifest.", False),
    "ARTIFACT_SECRET_DETECTED": FailureDefinition("ARTIFACT_SECURITY_FAILURE", "A generated artifact contains prohibited literal secret data.", False),
    "SEED_SCRIPT_SECURITY_VIOLATION": FailureDefinition("ARTIFACT_SECURITY_FAILURE", "The generated seed script violates the security policy.", False),
    "DEADLINE_EXCEEDED": FailureDefinition("EXECUTION_LIMIT", "The seed workflow deadline was exceeded.", False),
    "TOKEN_BUDGET_EXCEEDED": FailureDefinition("EXECUTION_LIMIT", "The seed workflow token budget was exceeded.", False),
    "GITHUB_AUTH_FAILED": FailureDefinition("AUTHORIZATION_FAILURE", "GitHub authentication failed.", False),
    "GITHUB_NOT_FOUND": FailureDefinition("ARTIFACT_TRANSFER_FAILURE", "The requested GitHub repository, branch, or path was not found.", False),
    "GITHUB_ARTIFACT_EXISTS": FailureDefinition("ARTIFACT_CONFLICT", "The requested GitHub artifact already exists.", False),
    "GITHUB_CONFLICT": FailureDefinition("ARTIFACT_CONFLICT", "The GitHub branch changed during the commit.", True),
    "GITHUB_RATE_LIMITED": FailureDefinition("ARTIFACT_TRANSFER_FAILURE", "GitHub rate limiting prevented the operation.", True),
    "GITHUB_UNREACHABLE": FailureDefinition("ARTIFACT_TRANSFER_FAILURE", "GitHub is temporarily unreachable.", True),
    "GITHUB_API_ERROR": FailureDefinition("ARTIFACT_TRANSFER_FAILURE", "The GitHub API request failed.", True),
    "GITHUB_INVALID_RESPONSE": FailureDefinition("ARTIFACT_TRANSFER_FAILURE", "GitHub returned an invalid response.", True),
    "GITHUB_CONTENT_MISMATCH": FailureDefinition("ARTIFACT_INTEGRITY_FAILURE", "GitHub content does not match the committed manifest.", False),
    "GITHUB_MANIFEST_INVALID": FailureDefinition("ARTIFACT_INTEGRITY_FAILURE", "The GitHub seed manifest is invalid.", False),
}

_URI_PATTERN = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_ARN_PATTERN = re.compile(r"\barn:aws(?:-[a-z]+)?:\S+")
_IP_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b")
_SECRET_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"
)
_STACK_LINE_PATTERN = re.compile(r"(?m)^\s*at\s+.*$")


def failure_definition(code: Any) -> FailureDefinition:
    """Return the public definition for a code, defaulting to infrastructure failure."""
    return FAILURES.get(str(code), FAILURES["VALIDATOR_INFRASTRUCTURE_FAILURE"])


def public_error(code: Any, raw_message: Any = None) -> dict[str, Any]:
    """Build one sanitized public error object from internal failure data."""
    public_code = str(code) if str(code) in FAILURES else "VALIDATOR_INFRASTRUCTURE_FAILURE"
    definition = failure_definition(public_code)
    message = definition.message
    if definition.preserve_safe_message and raw_message:
        sanitized = sanitize_message(raw_message)
        if sanitized:
            message = sanitized
    return {
        "code": public_code,
        "message": message,
        "retryable": definition.retryable,
        "detail": {"failure_class": definition.failure_class},
    }


def sanitize_message(value: Any) -> str:
    """Remove connection, credential, infrastructure, and stack details."""
    message = str(value or "")
    message = _STACK_LINE_PATTERN.sub("", message)
    message = _URI_PATTERN.sub("[redacted URI]", message)
    message = _ARN_PATTERN.sub("[redacted AWS ARN]", message)
    message = _IP_PATTERN.sub("[redacted network address]", message)
    message = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[redacted]", message)
    return " ".join(message.split())[:500]