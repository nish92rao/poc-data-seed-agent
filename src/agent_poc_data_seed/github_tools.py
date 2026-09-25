"""Shared names and safe result formatting for GitHub tools."""

from __future__ import annotations

GITHUB_TOOL_NAMES = frozenset(
    {
        "read_seed_input_from_github_tool",
        "commit_seed_bundle_to_github_tool",
        "read_seed_repair_source_from_github_tool",
        "validate_github_seed_bundle_tool",
    }
)