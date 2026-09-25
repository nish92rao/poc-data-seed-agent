"""Validation for Coding Orchestrator seed-generation envelopes."""

from __future__ import annotations

import json
import hashlib
import re
import secrets
import time
from dataclasses import replace
from dataclasses import dataclass
from typing import Any, Mapping

from agent_poc_data_seed.limits import MAX_TOKEN_BUDGET, parse_deadline, parse_max_tokens
from agent_poc_data_seed.artifact_layout import validation_report_key

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_ULID_PATTERN = r"[0-9A-HJKMNPQRSTVWXYZ]{26}"
_PRODUCTION_ID_PATTERNS = {
    "poc_id": re.compile(rf"^poc_{_ULID_PATTERN}$"),
    "run_id": re.compile(rf"^run_{_ULID_PATTERN}$"),
    "task_id": re.compile(rf"^task_{_ULID_PATTERN}$"),
}
_FORBIDDEN_SECRET_FIELDS = frozenset(
    {"mongodb_uri", "connection_uri", "connectionstring", "database_uri", "database_url", "db_uri", "api_key"}
)
_MAX_VALIDATION_ATTEMPTS = 3
_VERSION_PATTERN = re.compile(r"^v\d{3}$")
_REPAIRABLE_FAILURE_CLASSES = frozenset({"IMPLEMENTATION_FAILURE"})
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


@dataclass(frozen=True)
class FailureReport:
    """Sanitized repair evidence shared by internal and external repair paths."""

    component: str
    failure_class: str
    exit_code: int
    stderr_excerpt: str
    attempt: int
    max_attempts: int
    report_key: str


@dataclass(frozen=True)
class RepairSource:
    """Immutable identity of the seed bundle being repaired."""

    previous_code_version: str
    source_commit_sha: str


@dataclass(frozen=True)
class SeedGenerationEnvelope:
    """Validated fields that are safe to use for workflow selection."""

    poc_id: str
    run_id: str
    task_id: str
    trace_id: str
    spec_version: str | None
    code_version: str
    mode: str
    storage_mode: str
    validation_mode: str
    max_validation_attempts: int
    deadline_at: str = "9999-12-31T23:59:59+00:00"
    max_tokens: int = 1_000_000
    spec_commit_sha: str | None = None
    source_commit_sha: str | None = None
    branch_head_sha: str | None = None
    repair_source: RepairSource | None = None
    failure: FailureReport | None = None


def parse_seed_envelope(content: str) -> SeedGenerationEnvelope | None:
    """Parse a request with only ``request.poc_id`` required."""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, Mapping) and isinstance(payload.get("message"), str):
        try:
            nested = json.loads(payload["message"])
        except json.JSONDecodeError:
            nested = None
        if isinstance(nested, Mapping) and "request" in nested:
            raise ValueError("Paste the AgentEnvelope directly; the message and user_id wrapper is only for the HTTP invoke API")
    if not isinstance(payload, Mapping) or "request" not in payload:
        return None
    request = payload["request"]
    if not isinstance(request, Mapping):
        raise ValueError("request must be an object")
    _reject_secret_fields(payload)
    poc_id = _identifier(request.get("poc_id"), "poc_id")
    fingerprint = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    run_id = _correlation_id(request.get("run_id"), "run_id", fingerprint)
    task_id = _correlation_id(request.get("task_id"), "task_id", fingerprint)
    trace_value = request.get("trace_id")
    trace_id = trace_value if isinstance(trace_value, str) and _IDENTIFIER_PATTERN.fullmatch(trace_value) else f"trace_{run_id.removeprefix('run_')}"
    try:
        deadline = parse_deadline(request.get("deadline_at"))
    except ValueError:
        deadline = parse_deadline("9999-12-31T23:59:59Z")
    budget = request.get("budget")
    try:
        max_tokens = parse_max_tokens(budget.get("max_tokens") if isinstance(budget, Mapping) else None)
    except ValueError:
        max_tokens = MAX_TOKEN_BUDGET
    return SeedGenerationEnvelope(
        poc_id=poc_id,
        run_id=run_id,
        task_id=task_id,
        trace_id=trace_id,
        spec_version="v001",
        code_version="v001",
        mode="generate",
        storage_mode="github",
        validation_mode="generate_and_validate",
        max_validation_attempts=_MAX_VALIDATION_ATTEMPTS,
        deadline_at=deadline.isoformat(),
        max_tokens=max_tokens,
    )


def latest_seed_envelope(message_contents: list[Any]) -> SeedGenerationEnvelope | None:
    """Return the latest validated envelope across agent and tool message turns."""
    for content in reversed(message_contents):
        if isinstance(content, str):
            envelope = parse_seed_envelope(content)
            if envelope:
                return envelope
    return None


def fresh_seed_envelope(content: str) -> SeedGenerationEnvelope | None:
    """Normalize one invocation, assigning fresh missing correlation IDs once."""
    envelope = parse_seed_envelope(content)
    if envelope is None:
        return None
    payload = json.loads(content)
    request = payload["request"]
    run_id = request.get("run_id")
    task_id = request.get("task_id")
    fresh_run_id = run_id if isinstance(run_id, str) and _PRODUCTION_ID_PATTERNS["run_id"].fullmatch(run_id) else f"run_{_new_ulid()}"
    fresh_task_id = task_id if isinstance(task_id, str) and _PRODUCTION_ID_PATTERNS["task_id"].fullmatch(task_id) else f"task_{_new_ulid()}"
    trace_value = request.get("trace_id")
    trace_id = trace_value if isinstance(trace_value, str) and _IDENTIFIER_PATTERN.fullmatch(trace_value) else f"trace_{fresh_run_id.removeprefix('run_')}"
    return replace(envelope, run_id=fresh_run_id, task_id=fresh_task_id, trace_id=trace_id)


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{name} may contain only letters, numbers, underscores, and hyphens")
    return value


def _correlation_id(value: Any, name: str, fingerprint: str) -> str:
    pattern = _PRODUCTION_ID_PATTERNS[name]
    if isinstance(value, str) and pattern.fullmatch(value):
        return value
    digest = hashlib.sha256(f"{name}\0{fingerprint}".encode()).digest()[:16]
    encoded = _crockford_encode(int.from_bytes(digest, "big"), 26)
    prefix = {"run_id": "run", "task_id": "task"}[name]
    return f"{prefix}_{encoded}"


def _crockford_encode(value: int, length: int) -> str:
    characters = []
    for _ in range(length):
        characters.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(characters))


def _new_ulid() -> str:
    timestamp = int(time.time() * 1000) & ((1 << 48) - 1)
    randomness = int.from_bytes(secrets.token_bytes(10), "big")
    return _crockford_encode((timestamp << 80) | randomness, 26)


def _version(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _VERSION_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must use the vNNN format")
    return value


def _require_production_id(value: str, name: str) -> None:
    if not _PRODUCTION_ID_PATTERNS[name].fullmatch(value):
        raise ValueError(f"{name} must use its prefixed ULID format for GitHub requests")


def _require_strings(payload: Mapping[str, Any], *fields: str) -> None:
    for field in fields:
        if not isinstance(payload.get(field), str):
            raise ValueError(f"inputs.{field} must be a string")


def _validation_attempt_limit(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= _MAX_VALIDATION_ATTEMPTS:
        raise ValueError(f"max_validation_attempts must be an integer from 1 to {_MAX_VALIDATION_ATTEMPTS}")
    return value


def _github_repair_source(params: Mapping[str, Any], code_version: str) -> RepairSource:
    previous_code_version = _identifier(params.get("previous_code_version"), "previous_code_version")
    if previous_code_version == code_version:
        raise ValueError("code_version must differ from previous_code_version for repair")
    source = params.get("previous_source")
    if not isinstance(source, Mapping):
        raise ValueError("repair requires previous_source")
    source_commit_sha = _commit_sha(source.get("source_commit_sha"), "previous_source.source_commit_sha")
    return RepairSource(previous_code_version, source_commit_sha)


def _failure_report(value: Any, poc_id: str, run_id: str, code_version: str) -> FailureReport:
    if not isinstance(value, Mapping):
        raise ValueError("repair requires a failure report")
    component = value.get("component")
    failure_class = value.get("failure_class")
    exit_code = value.get("exit_code")
    stderr_excerpt = value.get("stderr_excerpt")
    attempt = value.get("attempt")
    max_attempts = value.get("max_attempts")
    report_key = value.get("report_key")
    if component != "seed" or failure_class not in _REPAIRABLE_FAILURE_CLASSES:
        raise ValueError("failure must identify the seed component and a repairable failure_class")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise ValueError("failure.exit_code must be an integer")
    if not isinstance(stderr_excerpt, str) or not stderr_excerpt or len(stderr_excerpt) > 2000:
        raise ValueError("failure.stderr_excerpt must be a non-empty string up to 2000 characters")
    if "mongodb" in stderr_excerpt.lower() or "://" in stderr_excerpt:
        raise ValueError("failure.stderr_excerpt must not contain connection data")
    if not isinstance(attempt, int) or not isinstance(max_attempts, int) or not 1 <= attempt <= max_attempts <= _MAX_VALIDATION_ATTEMPTS:
        raise ValueError("failure attempt values must be within the repair limit")
    if report_key != validation_report_key(poc_id, code_version, run_id):
        raise ValueError("failure.report_key must name the canonical source validation report")
    return FailureReport(component, failure_class, exit_code, stderr_excerpt, attempt, max_attempts, report_key)


def _commit_sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError(f"{name} must be a lowercase 40-character Git commit SHA")
    return value


def _reject_secret_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized_key = str(key).lower()
            if (
                normalized_key in _FORBIDDEN_SECRET_FIELDS
                or normalized_key == "secret"
                or normalized_key.endswith("_secret")
                or normalized_key == "password"
                or normalized_key.endswith("_password")
                or normalized_key == "token"
                or normalized_key.endswith("_token")
            ):
                raise ValueError("AgentEnvelope must not contain database credentials or secrets")
            _reject_secret_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_secret_fields(nested)
