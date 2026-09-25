"""Tests for deterministic seed workflow result routing."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from agent_poc_data_seed.github_tool_calls import normalize_github_tool_calls
from agent_poc_data_seed.workflow import decide_validation_only_result, decide_validation_result, next_code_version


class WorkflowTests(unittest.TestCase):
    def test_github_commit_uses_graph_owned_identity_and_head(self) -> None:
        response = AIMessage(
            content="",
            tool_calls=[{
                "name": "commit_seed_bundle_to_github_tool",
                "id": "commit",
                "type": "tool_call",
                "args": {
                    "poc_id": "wrong",
                    "run_id": "wrong",
                    "task_id": "wrong",
                    "trace_id": "mutated-trace",
                    "spec_version": "v999",
                    "code_version": "v999",
                    "spec_commit_sha": "wrong",
                    "expected_head_sha": "stale",
                    "seed_js": "seed",
                },
            }],
        )
        envelope = SimpleNamespace(
            poc_id="poc_owned",
            run_id="run_owned",
            task_id="task_owned",
            trace_id="trace_owned",
            spec_version="v001",
            spec_commit_sha="a" * 40,
            branch_head_sha="b" * 40,
            repair_source=None,
        )

        normalized = normalize_github_tool_calls(
            response,
            envelope,
            {
                "phase": "repair",
                "validation_attempts": 1,
                "current_code_version": "v005",
                "branch_head_sha": "c" * 40,
            },
        )

        self.assertEqual(normalized.tool_calls[0]["args"], {
            "poc_id": "poc_owned",
            "run_id": "run_owned",
            "task_id": "task_owned",
            "trace_id": "trace_owned",
            "spec_version": "v001",
            "code_version": "v005",
            "spec_commit_sha": "a" * 40,
            "expected_head_sha": "c" * 40,
            "seed_js": "seed",
        })

    def test_project_commit_uses_resolved_shared_context(self) -> None:
        response = AIMessage(content="", tool_calls=[{
            "name": "commit_seed_bundle_to_github_tool",
            "id": "commit",
            "type": "tool_call",
            "args": {"branch": "attacker", "data_model_commit_sha": "wrong", "seed_js": "seed"},
        }])
        envelope = SimpleNamespace(
            poc_id="1790237138344", run_id="run_owned", task_id="task_owned",
            trace_id="trace_owned", spec_version="v001", spec_commit_sha=None,
            branch_head_sha=None, repair_source=None,
        )
        context = {
            "branch": "owner/project",
            "data_model": {"commit_sha": "a" * 40},
            "query_patterns": {"commit_sha": "b" * 40},
            "defaults_applied": ["default"],
        }
        normalized = normalize_github_tool_calls(
            response,
            envelope,
            {"phase": "generate", "validation_attempts": 0, "current_code_version": "v004", "branch_head_sha": "c" * 40},
            context,
        )
        args = normalized.tool_calls[0]["args"]
        self.assertEqual(args["branch"], "owner/project")
        self.assertEqual(args["spec_commit_sha"], "")
        self.assertEqual(args["data_model_commit_sha"], "a" * 40)
        self.assertEqual(args["query_patterns_commit_sha"], "b" * 40)
        self.assertEqual(args["code_version"], "v004")
        self.assertEqual(args["expected_head_sha"], "c" * 40)

    def test_project_data_model_read_uses_query_commit_for_index_derivation(self) -> None:
        response = AIMessage(content="", tool_calls=[{
            "name": "read_seed_input_from_github_tool",
            "id": "read",
            "type": "tool_call",
            "args": {"artifact": "data_model", "source_commit_sha": "wrong"},
        }])
        envelope = SimpleNamespace(
            poc_id="1790237138344", run_id="run_owned", task_id="task_owned",
            trace_id="trace_owned", spec_version="v001", spec_commit_sha=None,
            branch_head_sha=None, repair_source=None,
        )
        context = {
            "branch": "owner/project",
            "data_model": {"path": "spec_architect/data_model.json", "commit_sha": "a" * 40},
            "query_patterns": {"path": "spec_architect/query_patterns.json", "commit_sha": "b" * 40},
        }
        normalized = normalize_github_tool_calls(
            response,
            envelope,
            {"phase": "generate", "validation_attempts": 0, "current_code_version": "v010"},
            context,
        )
        args = normalized.tool_calls[0]["args"]
        self.assertEqual(args["source_commit_sha"], "a" * 40)
        self.assertEqual(args["query_patterns_commit_sha"], "b" * 40)

    def test_combined_model_read_is_expanded_to_two_graph_owned_reads(self) -> None:
        response = AIMessage(content="", tool_calls=[{
            "name": "read_seed_input_from_github_tool",
            "id": "combined",
            "type": "tool_call",
            "args": {"artifact": "data_model,query_patterns", "source_commit_sha": None},
        }])
        envelope = SimpleNamespace(
            poc_id="1790237138344", run_id="run_owned", task_id="task_owned",
            trace_id="trace_owned", spec_version="v001", spec_commit_sha=None,
            branch_head_sha=None, repair_source=None,
        )
        context = {
            "branch": "owner/project",
            "data_model": {"path": "spec_architect/data_model.json", "commit_sha": "a" * 40},
            "query_patterns": {"path": "spec_architect/query_patterns.json", "commit_sha": "b" * 40},
        }
        normalized = normalize_github_tool_calls(
            response,
            envelope,
            {"phase": "generate", "validation_attempts": 0, "current_code_version": "v011"},
            context,
        )
        self.assertEqual([call["args"]["artifact"] for call in normalized.tool_calls], ["data_model", "query_patterns"])
        self.assertEqual(normalized.tool_calls[0]["args"]["source_commit_sha"], "a" * 40)
        self.assertEqual(normalized.tool_calls[1]["args"]["source_commit_sha"], "b" * 40)

    def test_successful_validation_completes_workflow(self) -> None:
        decision = decide_validation_result(
            {"status": "succeeded", "report_key": "pocs/poc_1/code/v001/seed/validation/run_1/report.json"},
            validation_attempts=1,
            max_validation_attempts=3,
        )
        self.assertEqual(decision.action, "succeeded")

    def test_next_code_version_rejects_exhaustion(self) -> None:
        self.assertEqual(next_code_version("v998"), "v999")
        with self.assertRaisesRegex(ValueError, "exhausted"):
            next_code_version("v999")

    def test_ordinary_validation_failure_requests_repair_before_limit(self) -> None:
        decision = decide_validation_result(
            {"status": "failed", "error": {"code": "VALIDATION_FAILED", "message": "Broken seed output"}},
            validation_attempts=1,
            max_validation_attempts=3,
        )
        self.assertEqual(decision.action, "repair")

    def test_blocked_failure_never_requests_repair(self) -> None:
        decision = decide_validation_result(
            {
                "status": "failed",
                "error": {"code": "VALIDATION_FAILED", "failure_class": "REQUEST_CONTRADICTION"},
            },
            validation_attempts=1,
            max_validation_attempts=3,
        )
        self.assertEqual(decision.action, "blocked")

    def test_final_attempt_exhausts_repairs(self) -> None:
        decision = decide_validation_result(
            {"status": "failed", "error": {"code": "VALIDATION_FAILED"}},
            validation_attempts=3,
            max_validation_attempts=3,
        )
        self.assertEqual(decision.action, "exhausted")

    def test_infrastructure_errors_do_not_trigger_repair(self) -> None:
        decision = decide_validation_result(
            {"status": "failed", "error": {"code": "VALIDATION_TIMEOUT"}},
            validation_attempts=1,
            max_validation_attempts=3,
        )
        self.assertEqual(decision.action, "infrastructure_error")

    def test_repairs_advance_version(self) -> None:
        self.assertEqual(next_code_version("v001"), "v002")
        with self.assertRaisesRegex(ValueError, "vNNN"):
            next_code_version("version_1")

    def test_validation_only_failure_is_terminal_without_repair(self) -> None:
        decision = decide_validation_only_result(
            {"status": "failed", "error": {"code": "ARTIFACT_SECRET_DETECTED", "failure_class": "ARTIFACT_SECURITY_FAILURE"}}
        )
        self.assertEqual(decision.status, "failed")
        self.assertEqual(decision.code, "ARTIFACT_SECRET_DETECTED")

    def test_validation_only_contradiction_is_blocked(self) -> None:
        decision = decide_validation_only_result(
            {"status": "failed", "error": {"code": "VALIDATION_FAILED", "failure_class": "REQUEST_CONTRADICTION"}}
        )
        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.code, "REQUEST_CONTRADICTION")


if __name__ == "__main__":
    unittest.main()