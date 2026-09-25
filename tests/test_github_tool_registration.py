"""Registration-only contract tests for the deferred Step 18 GitHub tools."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from agent_poc_data_seed.github_tools import GITHUB_TOOL_NAMES


class GitHubToolRegistrationTests(unittest.TestCase):
    def test_exact_tool_names_are_declared(self) -> None:
        self.assertEqual(
            GITHUB_TOOL_NAMES,
            {
                "read_seed_input_from_github_tool",
                "commit_seed_bundle_to_github_tool",
                "read_seed_repair_source_from_github_tool",
                "validate_github_seed_bundle_tool",
            },
        )

    def test_registration_defines_expected_argument_schemas(self) -> None:
        workspace = Path(__file__).resolve().parents[1]
        source = (workspace / "src/agent_poc_data_seed/tools.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        register = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "register")
        definitions = {
            node.name: tuple(argument.arg for argument in node.args.args)
            for node in register.body
            if isinstance(node, ast.FunctionDef)
        }
        expected = {
            "read_seed_input_from_github_tool": (
                "poc_id", "source_commit_sha", "artifact", "branch", "path", "spec_version",
                "query_patterns_commit_sha",
            ),
            "commit_seed_bundle_to_github_tool": (
                "poc_id", "run_id", "task_id", "trace_id", "spec_version", "code_version",
                "seed_js", "package_json", "seed_readme", "expected_head_sha",
                "spec_commit_sha", "branch", "data_model_commit_sha",
                "query_patterns_commit_sha", "defaults_json", "repair_notes", "repair_json",
            ),
            "read_seed_repair_source_from_github_tool": (
                "poc_id", "spec_version", "previous_code_version", "source_commit_sha", "branch",
            ),
            "validate_github_seed_bundle_tool": (
                "poc_id", "run_id", "task_id", "trace_id", "spec_version",
                "code_version", "source_commit_sha", "branch",
            ),
        }
        for name, arguments in expected.items():
            self.assertEqual(definitions[name], arguments)

    def test_agent_yaml_lists_each_tool_in_tool_sandbox(self) -> None:
        config = (Path(__file__).resolve().parents[1] / "agent.yaml").read_text(encoding="utf-8")
        for name in GITHUB_TOOL_NAMES:
            self.assertIn(f"- {name}", config)


if __name__ == "__main__":
    unittest.main()