"""Tests for the stable public seed failure taxonomy."""

from __future__ import annotations

import unittest

from agent_poc_data_seed.failures import FAILURES, public_error, sanitize_message


class FailureTaxonomyTests(unittest.TestCase):
    def test_required_public_codes_have_stable_behavior(self) -> None:
        self.assertTrue(FAILURES["VALIDATION_FAILED"].repairable)
        self.assertFalse(FAILURES["REQUEST_CONTRADICTION"].retryable)
        self.assertTrue(FAILURES["VALIDATION_TIMEOUT"].retryable)
        self.assertFalse(FAILURES["REPAIR_ATTEMPTS_EXHAUSTED"].repairable)
        self.assertTrue(FAILURES["SEED_SCRIPT_INVALID"].repairable)
        self.assertFalse(FAILURES["ARTIFACT_SECRET_DETECTED"].repairable)
        self.assertEqual(FAILURES["ARTIFACT_HASH_MISMATCH"].failure_class, "ARTIFACT_INTEGRITY_FAILURE")
        self.assertEqual(FAILURES["DEADLINE_EXCEEDED"].failure_class, "EXECUTION_LIMIT")
        self.assertFalse(FAILURES["TOKEN_BUDGET_EXCEEDED"].retryable)
        self.assertTrue(FAILURES["GITHUB_RATE_LIMITED"].retryable)
        self.assertFalse(FAILURES["GITHUB_CONTENT_MISMATCH"].retryable)
        self.assertEqual(FAILURES["GITHUB_MANIFEST_INVALID"].failure_class, "ARTIFACT_INTEGRITY_FAILURE")

    def test_sanitizer_removes_infrastructure_and_secret_details(self) -> None:
        raw = (
            "mongodb+srv://user:pass@example.test/db arn:aws:iam::123456789012:role/test "
            "10.2.3.4:27017 password=hunter2\n    at /var/task/handler.js:1:2"
        )
        sanitized = sanitize_message(raw)
        self.assertNotIn("user:pass", sanitized)
        self.assertNotIn("arn:aws", sanitized)
        self.assertNotIn("10.2.3.4", sanitized)
        self.assertNotIn("hunter2", sanitized)
        self.assertNotIn("/var/task", sanitized)

    def test_unknown_code_becomes_safe_infrastructure_failure(self) -> None:
        error = public_error("RAW_NODE_ERROR", "ECONNREFUSED 10.2.3.4:27017")
        self.assertEqual(error["code"], "VALIDATOR_INFRASTRUCTURE_FAILURE")
        self.assertTrue(error["retryable"])
        self.assertNotIn("10.2.3.4", error["message"])


if __name__ == "__main__":
    unittest.main()