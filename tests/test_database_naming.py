"""Tests for database-name normalization shared with the validator contract."""

from __future__ import annotations

import unittest

from agent_poc_data_seed.database_naming import target_database_name


class DatabaseNamingTests(unittest.TestCase):
    def test_short_poc_id_is_the_actual_seed_database_name(self) -> None:
        self.assertEqual(target_database_name("poc_subscription_billing_001"), "poc_subscription_billing_001")

    def test_overlong_poc_id_is_deterministically_shortened(self) -> None:
        poc_id = "poc_a_very_long_identifier_that_exceeds_mongodb_database_name_limits"
        name = target_database_name(poc_id)
        self.assertLessEqual(len(name.encode("utf-8")), 38)
        self.assertEqual(name, target_database_name(poc_id))
        self.assertNotEqual(name, target_database_name(f"{poc_id}_other"))


if __name__ == "__main__":
    unittest.main()