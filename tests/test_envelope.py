"""Tests for minimal shared-state-driven seed invocation envelopes."""

from __future__ import annotations

import json
import re
import unittest

from agent_poc_data_seed.envelope import fresh_seed_envelope, latest_seed_envelope, parse_seed_envelope
from agent_poc_data_seed.system_message import GENERATE_AND_VALIDATE_GITHUB_PROMPT, system_prompt_for

ULID = "[0-9A-HJKMNPQRSTVWXYZ]{26}"


class SeedEnvelopeTests(unittest.TestCase):
    def test_only_poc_id_is_required_and_defaults_are_replay_stable(self) -> None:
        content = json.dumps({"request": {"poc_id": "1790237138344"}})
        first = parse_seed_envelope(content)
        second = parse_seed_envelope(content)
        self.assertEqual(first, second)
        assert first is not None
        self.assertRegex(first.run_id, rf"^run_{ULID}$")
        self.assertRegex(first.task_id, rf"^task_{ULID}$")
        self.assertRegex(first.trace_id, rf"^trace_{ULID}$")
        self.assertEqual(first.storage_mode, "github")
        self.assertEqual(first.validation_mode, "generate_and_validate")
        self.assertEqual(first.max_validation_attempts, 3)
        self.assertEqual(first.max_tokens, 1_000_000)
        self.assertEqual(first.deadline_at, "9999-12-31T23:59:59+00:00")

    def test_preserves_valid_correlation_and_limits(self) -> None:
        payload = {
            "request": {
                "poc_id": "poc_01J8Q8MFYJ6NVJ8B2Q5V2D9Q1A",
                "run_id": "run_01J8Q8MFYJ6NVJ8B2Q5V2D9Q1B",
                "task_id": "task_01J8Q8MFYJ6NVJ8B2Q5V2D9Q1C",
                "trace_id": "trace_caller",
                "deadline_at": "2099-01-01T00:00:00Z",
                "budget": {"max_tokens": 25000},
            }
        }
        parsed = parse_seed_envelope(json.dumps(payload))
        assert parsed is not None
        self.assertEqual(parsed.run_id, payload["request"]["run_id"])
        self.assertEqual(parsed.task_id, payload["request"]["task_id"])
        self.assertEqual(parsed.trace_id, "trace_caller")
        self.assertEqual(parsed.max_tokens, 25000)
        self.assertEqual(parsed.deadline_at, "2099-01-01T00:00:00+00:00")

    def test_separate_invocations_get_fresh_default_correlation(self) -> None:
        content = json.dumps({"request": {"poc_id": "1790237138344"}})
        first = fresh_seed_envelope(content)
        second = fresh_seed_envelope(content)
        assert first is not None and second is not None
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertNotEqual(first.task_id, second.task_id)
        self.assertEqual(first.trace_id, f"trace_{first.run_id.removeprefix('run_')}")

    def test_fresh_normalization_preserves_valid_caller_ids(self) -> None:
        payload = {
            "request": {
                "poc_id": "1790237138344",
                "run_id": "run_01J8Q8MFYJ6NVJ8B2Q5V2D9Q1B",
                "task_id": "task_01J8Q8MFYJ6NVJ8B2Q5V2D9Q1C",
                "trace_id": "trace_caller",
            }
        }
        parsed = fresh_seed_envelope(json.dumps(payload))
        assert parsed is not None
        self.assertEqual(parsed.run_id, payload["request"]["run_id"])
        self.assertEqual(parsed.task_id, payload["request"]["task_id"])
        self.assertEqual(parsed.trace_id, "trace_caller")

    def test_malformed_optional_fields_are_normalized_not_rejected(self) -> None:
        parsed = parse_seed_envelope(json.dumps({
            "request": {
                "poc_id": "1790237138344",
                "run_id": "bad",
                "task_id": None,
                "trace_id": "contains spaces",
                "deadline_at": "bad",
                "budget": {"max_tokens": -1},
                "mode": "nonsense",
                "params": {"storage_mode": "s3", "code_version": "broken"},
            }
        }))
        assert parsed is not None
        self.assertTrue(re.fullmatch(rf"run_{ULID}", parsed.run_id))
        self.assertTrue(re.fullmatch(rf"task_{ULID}", parsed.task_id))
        self.assertEqual(parsed.mode, "generate")
        self.assertEqual(parsed.storage_mode, "github")
        self.assertEqual(parsed.code_version, "v001")

    def test_rejects_missing_or_unsafe_poc_id(self) -> None:
        for payload in ({"request": {}}, {"request": {"poc_id": "../other"}}):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "poc_id"):
                parse_seed_envelope(json.dumps(payload))

    def test_rejects_http_wrapper_and_visible_secrets(self) -> None:
        wrapped = json.dumps({"message": json.dumps({"request": {"poc_id": "123"}}), "user_id": "local"})
        with self.assertRaisesRegex(ValueError, "AgentEnvelope directly"):
            parse_seed_envelope(wrapped)
        with self.assertRaisesRegex(ValueError, "credentials or secrets"):
            parse_seed_envelope(json.dumps({"request": {"poc_id": "123", "params": {"mongodb_uri": "secret"}}}))

    def test_latest_envelope_survives_tool_messages(self) -> None:
        content = json.dumps({"request": {"poc_id": "1790237138344"}})
        parsed = latest_seed_envelope([content, '{"status":"succeeded"}'])
        assert parsed is not None
        self.assertEqual(parsed.poc_id, "1790237138344")

    def test_non_json_chat_and_unrelated_json_are_not_envelopes(self) -> None:
        self.assertIsNone(parse_seed_envelope("Please generate seed data"))
        self.assertIsNone(parse_seed_envelope('{"hello":"world"}'))

    def test_normalized_envelope_selects_bounded_github_prompt(self) -> None:
        parsed = parse_seed_envelope(json.dumps({"request": {"poc_id": "1790237138344"}}))
        assert parsed is not None
        self.assertEqual(
            system_prompt_for(parsed.storage_mode, parsed.validation_mode, parsed.mode),
            GENERATE_AND_VALIDATE_GITHUB_PROMPT,
        )


if __name__ == "__main__":
    unittest.main()
