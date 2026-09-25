"""Tests for safe, fail-open harness memory writes."""

from __future__ import annotations

import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from agent_poc_data_seed.memory import (
    event_id,
    procedure_name,
    save_episode,
    save_repair_pattern,
    successful_repair_pattern,
)


class HarnessMemoryTests(unittest.TestCase):
    def test_episode_is_run_keyed_sanitized_and_deterministic(self) -> None:
        memory = Mock()
        memory.save_episode.return_value = SimpleNamespace(acknowledged=True)

        saved = save_episode(
            memory,
            run_id="run_123",
            poc_id="poc_123",
            event="tool_completed",
            details={
                "discriminator": "call/1",
                "tool": "validate_github_seed_bundle_tool",
                "failure_code": "mongodb://user:pass@example.test/db",
                "ignored_secret": "do-not-store",
            },
        )

        self.assertTrue(saved)
        kwargs = memory.save_episode.call_args.kwargs
        content = json.loads(kwargs["content"])
        self.assertEqual(content["event_id"], event_id("run_123", "tool_completed", "call/1"))
        self.assertNotIn("ignored_secret", content)
        self.assertNotIn("mongodb://", kwargs["content"])
        self.assertEqual(kwargs["metadata"]["run_id"], "run_123")

    def test_successful_repair_uses_upserted_procedural_metadata(self) -> None:
        memory = Mock()
        memory.save_procedure.return_value = True

        saved = save_repair_pattern(
            memory,
            run_id="run_123",
            poc_id="poc_123",
            failure_code="SEED_SCRIPT_INVALID",
            failure_class="IMPLEMENTATION_FAILURE",
            source_version="v001",
            repaired_version="v002",
            attempt=2,
            summary="Removed mongodb://user:pass@example.test/db from output.",
        )

        self.assertTrue(saved)
        kwargs = memory.save_procedure.call_args.kwargs
        self.assertEqual(kwargs["procedure"], procedure_name("SEED_SCRIPT_INVALID", "IMPLEMENTATION_FAILURE"))
        self.assertEqual(kwargs["procedure"], "poc-data-seed-repair-seed-script-invalid-implementation-failure")
        self.assertEqual(kwargs["metadata"]["failure_signature"], "SEED_SCRIPT_INVALID::IMPLEMENTATION_FAILURE")
        self.assertTrue(kwargs["update_existing"])
        self.assertNotIn("mongodb://", kwargs["content"])
        self.assertEqual(set(kwargs["steps"][0]), {"step_type", "content", "description"})

    def test_long_procedure_name_is_bounded_and_deterministic(self) -> None:
        first = procedure_name("EXTERNAL_FAILURE_REPORT", "IMPLEMENTATION_FAILURE")
        second = procedure_name("EXTERNAL_FAILURE_REPORT", "IMPLEMENTATION_FAILURE")
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 64)
        self.assertRegex(first, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

    def test_memory_failures_do_not_escape(self) -> None:
        memory = Mock()
        memory.save_episode.side_effect = RuntimeError("token=secret")
        self.assertFalse(save_episode(memory, run_id="run_1", poc_id="poc_1", event="started", details={}))

    def test_derives_internal_repair_pattern_from_prior_failure(self) -> None:
        pattern = successful_repair_pattern(
            current_version="v002",
            validation_attempts=1,
            prior_validation={"error": {"code": "SEED_SCRIPT_INVALID", "failure_class": "IMPLEMENTATION_FAILURE"}},
        )
        self.assertIsNotNone(pattern)
        self.assertEqual(pattern.failure_code, "SEED_SCRIPT_INVALID")
        self.assertEqual(pattern.source_version, "v001")
        self.assertEqual(pattern.attempt, 2)

    def test_derives_external_repair_pattern_from_envelope_provenance(self) -> None:
        pattern = successful_repair_pattern(
            current_version="v004",
            validation_attempts=0,
            external_failure={"failure_class": "IMPLEMENTATION_FAILURE", "attempt": 2},
            previous_code_version="v002",
        )
        self.assertIsNotNone(pattern)
        self.assertEqual(pattern.failure_code, "EXTERNAL_FAILURE_REPORT")
        self.assertEqual(pattern.source_version, "v002")
        self.assertEqual(pattern.attempt, 2)

    def test_first_pass_success_does_not_create_repair_pattern(self) -> None:
        self.assertIsNone(successful_repair_pattern(current_version="v001", validation_attempts=0))

    def test_runtime_configuration_enables_required_memory_types(self) -> None:
        workspace = Path(__file__).resolve().parents[1]
        agent_config = (workspace / "agent.yaml").read_text(encoding="utf-8")
        project_config = (workspace.parent.parent / "project-config.yaml").read_text(encoding="utf-8")

        self.assertIn("memory: true", agent_config)
        self.assertIn("- episodic", project_config)
        self.assertIn("- procedural", project_config)


if __name__ == "__main__":
    unittest.main()