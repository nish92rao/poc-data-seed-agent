"""Tests for GitHub bundle checks before immutable writes."""

from __future__ import annotations

import json
import sys
import types
import unittest
from unittest.mock import patch

sdk = types.ModuleType("magenta_sdklanggraph")
sdk.App = type("App", (), {})
sys.modules.setdefault("magenta_sdklanggraph", sdk)

from agent_poc_data_seed.tools import register


class FakeApp:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, **_kwargs: object):
        def decorator(function):
            self.tools[function.__name__] = function
            return function
        return decorator


class GitHubCommitPreflightTests(unittest.TestCase):
    def test_invalid_seed_is_rejected_before_github_access(self) -> None:
        app = FakeApp()
        register(app)  # type: ignore[arg-type]
        commit = app.tools["commit_seed_bundle_to_github_tool"]
        incomplete_seed = """
const { MongoClient } = require("mongodb");
const uri = process.env.MONGODB_URI;
const database = process.env.DB_NAME;
"""
        package_json = '{"scripts":{"seed":"node seed.js"},"dependencies":{"mongodb":"^6.0.0"}}'

        with patch("agent_poc_data_seed.tools._github_store") as store:
            result = json.loads(commit(  # type: ignore[operator]
                "poc_1", "run_1", "task_1", "trace_1", "v001", "v010",
                incomplete_seed, package_json, "readme", "a" * 40, "b" * 40,
            ))

        store.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "SEED_SCRIPT_INVALID")
        self.assertIn("SEED_MAX_DOCS, SEED_COLLECTION_CAPS, seed_summary", result["error"]["message"])


if __name__ == "__main__":
    unittest.main()
