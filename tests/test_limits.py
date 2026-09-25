"""Tests for workflow deadline and token-budget enforcement."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from agent_poc_data_seed.limits import extract_usage, limit_failure, parse_deadline, parse_max_tokens


class LimitsTests(unittest.TestCase):
    def test_parses_timezone_deadline_and_rejects_naive_value(self) -> None:
        self.assertEqual(parse_deadline("2027-01-01T00:00:00Z").tzinfo, timezone.utc)
        with self.assertRaisesRegex(ValueError, "timezone"):
            parse_deadline("2027-01-01T00:00:00")

    def test_validates_positive_token_budget(self) -> None:
        self.assertEqual(parse_max_tokens(25000), 25000)
        for value in (0, -1, True, "100"):
            with self.assertRaisesRegex(ValueError, "max_tokens"):
                parse_max_tokens(value)

    def test_limit_decision_prioritizes_deadline_then_tokens(self) -> None:
        now = datetime(2027, 1, 2, tzinfo=timezone.utc)
        self.assertEqual(limit_failure(datetime(2027, 1, 1, tzinfo=timezone.utc), 100, 100, now=now), "DEADLINE_EXCEEDED")
        self.assertEqual(limit_failure(datetime(2027, 1, 3, tzinfo=timezone.utc), 100, 100, now=now), "TOKEN_BUDGET_EXCEEDED")

    def test_extracts_usage_when_provider_reports_it(self) -> None:
        self.assertEqual(extract_usage(SimpleNamespace(usage_metadata={"input_tokens": 12, "output_tokens": 3})), (12, 3, True))
        self.assertEqual(extract_usage(SimpleNamespace()), (0, 0, False))


if __name__ == "__main__":
    unittest.main()