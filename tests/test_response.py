"""Tests for the orchestrator-facing AgentEnvelope response wrapper."""

from __future__ import annotations

import unittest

from agent_poc_data_seed.envelope import SeedGenerationEnvelope
from agent_poc_data_seed.response import format_invalid_request_response, format_terminal_response


class ResponseTests(unittest.TestCase):
    envelope = SeedGenerationEnvelope("poc_1", "run_1", "task_1", "trace_1", "v001", "v001", "generate", "github", "generate_and_validate", 3)

    def test_success_uses_response_wrapper_and_safe_artifacts(self) -> None:
        response = format_terminal_response(
            self.envelope,
            {
                "status": "succeeded",
                "code_version": "v001",
                "target_database_name": "poc_1",
                "artifacts": [{"key": "pocs/poc_1/code/v001/seed/seed.js", "sha256": "a" * 64, "bytes": 10}],
                "validation": {"status": "succeeded", "report_key": "pocs/poc_1/code/v001/seed/validation/run_1/report.json"},
                "timeline": [{
                    "event_id": "run_1__workflow_completed__succeeded",
                    "timestamp": "2026-09-24T00:00:00+00:00",
                    "event_type": "workflow_completed",
                    "phase": "complete",
                    "poc_id": "poc_1",
                    "run_id": "run_1",
                    "task_id": "task_1",
                    "trace_id": "trace_1",
                    "code_version": "v001",
                    "validation_attempt": 1,
                    "status": "succeeded",
                    "details": {"final_code_version": "v001", "total_attempts": 1},
                }],
            },
        )["response"]
        self.assertEqual(response["status"], "succeeded")
        self.assertEqual(response["task_id"], "task_1")
        self.assertIsNone(response["error"])
        self.assertEqual([artifact["kind"] for artifact in response["artifacts"]], ["code", "report"])
        self.assertEqual(response["result"]["timeline"]["attempted_versions"], ["v001"])

    def test_blocked_uses_sanitized_error_wrapper(self) -> None:
        response = format_terminal_response(
            self.envelope,
            {
                "status": "blocked",
                "code": "REQUEST_CONTRADICTION",
                "validation": {"error": {"code": "VALIDATION_FAILED", "failure_class": "REQUEST_CONTRADICTION", "message": "mongodb+srv://secret"}},
            },
        )["response"]
        self.assertEqual(response["error"]["code"], "REQUEST_CONTRADICTION")
        self.assertNotIn("mongodb+srv", response["error"]["message"])
        self.assertFalse(response["error"]["retryable"])

    def test_retryable_github_failure_preserves_stable_shape(self) -> None:
        response = format_terminal_response(
            self.envelope,
            {"status": "failed", "code": "GITHUB_UNREACHABLE", "error": {"message": "connection failed", "retryable": True}},
        )["response"]
        self.assertEqual(response["error"]["code"], "GITHUB_UNREACHABLE")
        self.assertTrue(response["error"]["retryable"])
        self.assertEqual(set(response), {"task_id", "status", "result", "artifacts", "error", "usage"})

    def test_validation_attempts_are_preserved_in_error_detail(self) -> None:
        response = format_terminal_response(
            self.envelope,
            {
                "status": "failed",
                "code": "ARTIFACT_SECRET_DETECTED",
                "validation_attempts": 1,
                "validation": {"error": {"code": "ARTIFACT_SECRET_DETECTED"}},
            },
        )["response"]
        self.assertEqual(response["error"]["detail"]["validation_attempts"], 1)

    def test_reported_usage_is_preserved(self) -> None:
        usage = {"input_tokens": 120, "output_tokens": 30, "duration_ms": 450}
        response = format_terminal_response(
            self.envelope,
            {"status": "failed", "code": "TOKEN_BUDGET_EXCEEDED", "usage": usage},
        )["response"]
        self.assertEqual(response["usage"], usage)
        self.assertEqual(response["error"]["detail"]["failure_class"], "EXECUTION_LIMIT")

    def test_invalid_request_uses_same_wrapper_without_artifacts(self) -> None:
        response = format_invalid_request_response(
            '{"request":{"task_id":"task_readable"}}',
            ValueError("run_id must use its prefixed ULID format for GitHub requests"),
        )["response"]
        self.assertEqual(response["task_id"], "task_readable")
        self.assertEqual(response["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(response["artifacts"], [])
        self.assertFalse(response["error"]["retryable"])


if __name__ == "__main__":
    unittest.main()