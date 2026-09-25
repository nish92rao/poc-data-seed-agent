"""Bounded lifecycle events captured by the Magenta harness and caller response."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from agent_poc_data_seed.failures import sanitize_message

MAX_TIMELINE_EVENTS = 40
MAX_TIMELINE_BYTES = 32 * 1024
TIMELINE_LOG_MARKER = "seed_timeline_event"

_EVENT_TYPES = frozenset(
    {
        "run_started",
        "generation_started",
        "generation_completed",
        "artifact_uploaded",
        "manifest_written",
        "validation_started",
        "validation_result",
        "validation_decision",
        "repair_requested",
        "repair_completed",
        "workflow_completed",
        "workflow_failed",
    }
)
_DETAIL_FIELDS = frozenset(
    {
        "artifact_kind",
        "artifact_key",
        "sha256",
        "bytes",
        "source_code_version",
        "new_code_version",
        "final_code_version",
        "decision",
        "failure_code",
        "failure_class",
        "report_key",
        "max_validation_attempts",
        "total_attempts",
        "duration_ms",
        "source_commit_sha",
        "report_commit_sha",
    }
)
_ID_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_VERSION_PATTERN = re.compile(r"^v\d{3}$")


def timeline_event(
    *,
    event_type: str,
    discriminator: str,
    poc_id: str,
    run_id: str,
    task_id: str,
    trace_id: str,
    code_version: str,
    phase: str,
    validation_attempt: int,
    status: str,
    details: Mapping[str, Any] | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Create one strictly allowlisted lifecycle event."""
    if event_type not in _EVENT_TYPES:
        raise ValueError(f"Unsupported timeline event type: {event_type}")
    event_id = "__".join(
        _ID_PATTERN.sub("_", part).strip("_")
        for part in (run_id, event_type, discriminator)
        if part
    )
    event = {
        "event_id": event_id[:200],
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "phase": phase,
        "poc_id": poc_id,
        "run_id": run_id,
        "task_id": task_id,
        "trace_id": trace_id,
        "code_version": code_version,
        "validation_attempt": max(0, validation_attempt),
        "status": _safe_text(status, 40),
        "details": _safe_details(details or {}),
    }
    return event


def merge_timeline_events(
    current: list[dict[str, Any]],
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge events by deterministic ID and preserve first-seen order on replay."""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for candidate in [*current, *updates]:
        event = sanitize_timeline_event(candidate)
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in merged:
            continue
        merged[event_id] = event
        order.append(event_id)
    result: list[dict[str, Any]] = []
    size = 0
    for event_id in order[:MAX_TIMELINE_EVENTS]:
        event = {**merged[event_id], "sequence": len(result)}
        encoded_size = len(json.dumps(event, separators=(",", ":")).encode("utf-8"))
        if size + encoded_size > MAX_TIMELINE_BYTES:
            break
        result.append(event)
        size += encoded_size
    return result


def format_timeline(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Build the bounded caller-facing timeline with attempted versions."""
    source = list(events)
    bounded = merge_timeline_events([], [dict(event) for event in source])
    attempted_versions: list[str] = []
    for event in bounded:
        details = event.get("details")
        if isinstance(details, Mapping):
            for field in ("source_code_version",):
                value = details.get(field)
                if isinstance(value, str) and value not in attempted_versions:
                    attempted_versions.append(value)
        version = event.get("code_version")
        if isinstance(version, str) and version not in attempted_versions:
            attempted_versions.append(version)
        if isinstance(details, Mapping):
            value = details.get("new_code_version")
            if isinstance(value, str) and value not in attempted_versions:
                attempted_versions.append(value)
    valid_ids = {
        event.get("event_id")
        for event in source
        if isinstance(event, Mapping) and sanitize_timeline_event(event).get("event_id")
    }
    return {
        "events": bounded,
        "attempted_versions": attempted_versions,
        "last_sequence": len(bounded) - 1,
        "truncated": len(bounded) < len(valid_ids),
    }


def log_timeline_event(logger: logging.Logger, event: Mapping[str, Any]) -> None:
    """Emit one canonical JSON line through the harness-captured agent logger."""
    safe = sanitize_timeline_event(event)
    logger.info("%s %s", TIMELINE_LOG_MARKER, json.dumps(safe, sort_keys=True, separators=(",", ":")))


def sanitize_timeline_event(value: Mapping[str, Any]) -> dict[str, Any]:
    """Discard fields outside the public timeline contract."""
    event_type = str(value.get("event_type", ""))
    if event_type not in _EVENT_TYPES:
        return {}
    safe = {
        "event_id": _safe_text(value.get("event_id"), 200),
        "timestamp": _safe_text(value.get("timestamp"), 40),
        "event_type": event_type,
        "phase": _safe_text(value.get("phase"), 30),
        "poc_id": _safe_text(value.get("poc_id"), 120),
        "run_id": _safe_text(value.get("run_id"), 120),
        "task_id": _safe_text(value.get("task_id"), 120),
        "trace_id": _safe_text(value.get("trace_id"), 200),
        "code_version": _safe_version(value.get("code_version")),
        "validation_attempt": _safe_nonnegative_int(value.get("validation_attempt")),
        "status": _safe_text(value.get("status"), 40),
        "details": _safe_details(value.get("details") if isinstance(value.get("details"), Mapping) else {}),
    }
    sequence = value.get("sequence")
    if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 0:
        safe["sequence"] = sequence
    return safe


def _safe_details(details: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key in _DETAIL_FIELDS:
        value = details.get(key)
        if value is None:
            continue
        if key in {"bytes", "max_validation_attempts", "total_attempts", "duration_ms"}:
            safe[key] = _safe_nonnegative_int(value)
        elif key in {"source_code_version", "new_code_version", "final_code_version"}:
            safe[key] = _safe_version(value)
        elif key == "sha256":
            if isinstance(value, str) and _SHA256_PATTERN.fullmatch(value):
                safe[key] = value
        elif key in {"source_commit_sha", "report_commit_sha"}:
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value):
                safe[key] = value
        elif key in {"artifact_key", "report_key"}:
            if isinstance(value, str) and value.startswith(("pocs/", "generated/")) and ".." not in value and "://" not in value:
                safe[key] = value[:500]
        else:
            safe[key] = _safe_text(value, 120)
    return safe


def _safe_text(value: Any, limit: int) -> str:
    return sanitize_message(value)[:limit]


def _safe_version(value: Any) -> str:
    return str(value) if isinstance(value, str) and _VERSION_PATTERN.fullmatch(value) else ""


def _safe_nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0