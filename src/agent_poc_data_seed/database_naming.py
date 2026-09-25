"""Canonical MongoDB database names for seed execution and validation."""

from __future__ import annotations

import hashlib
import re

_MAX_DATABASE_NAME_BYTES = 38
_INVALID_DATABASE_NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9_]")


def target_database_name(poc_id: str) -> str:
    """Derive a stable MongoDB-safe target database name from a POC identifier."""
    normalized = _INVALID_DATABASE_NAME_CHARACTERS.sub("_", poc_id)
    if len(normalized.encode("utf-8")) <= _MAX_DATABASE_NAME_BYTES:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{normalized[:25]}_{digest}"