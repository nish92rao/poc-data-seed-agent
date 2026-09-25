"""Deadline and token-budget enforcement for seed workflows."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

MAX_TOKEN_BUDGET = 1_000_000


def parse_deadline(value: Any) -> datetime:
    """Parse an ISO-8601 deadline that includes timezone information."""
    if not isinstance(value, str) or not value:
        raise ValueError("deadline_at must be an ISO-8601 timestamp with timezone")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        deadline = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("deadline_at must be an ISO-8601 timestamp with timezone") from error
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError("deadline_at must be an ISO-8601 timestamp with timezone")
    return deadline.astimezone(timezone.utc)


def parse_max_tokens(value: Any) -> int:
    """Validate the positive workflow token budget."""
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_TOKEN_BUDGET:
        raise ValueError(f"budget.max_tokens must be an integer from 1 to {MAX_TOKEN_BUDGET}")
    return value


def limit_failure(
    deadline: datetime,
    max_tokens: int,
    used_tokens: int,
    *,
    now: datetime | None = None,
) -> str | None:
    """Return the stable terminal code when no new workflow phase may begin."""
    current = now or datetime.now(timezone.utc)
    if current >= deadline:
        return "DEADLINE_EXCEEDED"
    if used_tokens >= max_tokens:
        return "TOKEN_BUDGET_EXCEEDED"
    return None


def extract_usage(message: Any) -> tuple[int, int, bool]:
    """Extract provider-reported token usage without inventing unavailable values."""
    usage = getattr(message, "usage_metadata", None)
    if not isinstance(usage, Mapping):
        response_metadata = getattr(message, "response_metadata", None)
        token_usage = response_metadata.get("token_usage") if isinstance(response_metadata, Mapping) else None
        usage = token_usage if isinstance(token_usage, Mapping) else None
    if not isinstance(usage, Mapping):
        return 0, 0, False
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return 0, 0, False
    return max(input_tokens, 0), max(output_tokens, 0), True


def elapsed_milliseconds(started_at: str, *, now: datetime | None = None) -> int:
    """Return nonnegative elapsed workflow time for the response usage object."""
    started = datetime.fromisoformat(started_at)
    current = now or datetime.now(timezone.utc)
    return max(0, int((current - started).total_seconds() * 1000))