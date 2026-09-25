"""Tests for bounded, correlated seed lifecycle observability."""

from __future__ import annotations

import io
import json
import logging
import unittest

from agent_poc_data_seed.observability import (
    MAX_TIMELINE_EVENTS,
    format_timeline,
    log_timeline_event,
    merge_timeline_events,
    timeline_event,
)


def event(discriminator: str = "one", **overrides: object) -> dict[str, object]:
    values = {
        "event_type": "artifact_uploaded",
        "discriminator": discriminator,
        "poc_id": "poc_1",
        "run_id": "run_1",
        "task_id": "task_1",
        "trace_id": "trace_1",
        "code_version": "v001",
        "phase": "generate",
        "validation_attempt": 0,
        "status": "succeeded",
        "details": {
            "artifact_key": "pocs/poc_1/code/v001/seed/seed.js",
            "sha256": "a" * 64,
            "bytes": 10,
            "github_token": "secret-token",
            "content": "do-not-store",
        },
        "timestamp": "2026-09-24T00:00:00+00:00",
    }
    values.update(overrides)
    return timeline_event(**values)  # type: ignore[arg-type]


class ObservabilityTests(unittest.TestCase):
    def test_event_has_correlation_and_strict_safe_details(self) -> None:
        result = event()
        self.assertEqual(result["run_id"], "run_1")
        self.assertEqual(result["task_id"], "task_1")
        self.assertEqual(result["trace_id"], "trace_1")
        self.assertEqual(result["details"]["sha256"], "a" * 64)
        self.assertNotIn("github_token", result["details"])
        self.assertNotIn("content", result["details"])

    def test_merge_deduplicates_replay_and_bounds_events(self) -> None:
        first = event("same")
        merged = merge_timeline_events([first], [first, *[event(str(index)) for index in range(MAX_TIMELINE_EVENTS + 5)]])
        self.assertEqual(len(merged), MAX_TIMELINE_EVENTS)
        self.assertEqual(len({item["event_id"] for item in merged}), len(merged))
        self.assertEqual([item["sequence"] for item in merged], list(range(len(merged))))

    def test_format_timeline_reconstructs_attempted_versions(self) -> None:
        repaired = event(
            "repair",
            event_type="repair_requested",
            code_version="v002",
            details={"source_code_version": "v001", "new_code_version": "v002"},
        )
        result = format_timeline([repaired])
        self.assertEqual(result["attempted_versions"], ["v001", "v002"])
        self.assertFalse(result["truncated"])

    def test_duplicate_replay_does_not_mark_timeline_truncated(self) -> None:
        repeated = event("same")
        result = format_timeline([repeated, repeated])
        self.assertEqual(len(result["events"]), 1)
        self.assertFalse(result["truncated"])

    def test_logger_emits_one_canonical_json_event(self) -> None:
        stream = io.StringIO()
        logger = logging.getLogger("test.seed.timeline")
        logger.handlers = [logging.StreamHandler(stream)]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        log_timeline_event(logger, event())
        marker, payload = stream.getvalue().strip().split(" ", 1)
        self.assertEqual(marker, "seed_timeline_event")
        self.assertEqual(json.loads(payload)["event_id"], event()["event_id"])

    def test_supports_complete_lifecycle_taxonomy(self) -> None:
        event_types = {
            "run_started",
            "generation_started",
            "generation_completed",
            "artifact_uploaded",
            "manifest_written",
            "validation_started",
            "validation_result",
            "validation_decision",
            "repair_requested",
            "repair_completed",
            "workflow_completed",
            "workflow_failed",
        }
        for event_type in event_types:
            self.assertEqual(event(event_type, event_type=event_type)["event_type"], event_type)
        with self.assertRaisesRegex(ValueError, "Unsupported timeline event"):
            event("unknown", event_type="raw_log")


if __name__ == "__main__":
    unittest.main()