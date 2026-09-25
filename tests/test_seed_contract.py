"""Tests for deterministic pre-commit seed contract checks."""

from __future__ import annotations

import unittest

from agent_poc_data_seed.seed_contract import SeedContractError, validate_seed_bundle_text


VALID_PACKAGE = '{"scripts":{"seed":"node seed.js"},"dependencies":{"mongodb":"^6.0.0"}}'
VALID_SEED = """
const { MongoClient } = require("mongodb");
const uri = process.env.MONGODB_URI;
const database = process.env.DB_NAME;
const maxDocs = process.env.SEED_MAX_DOCS;
const caps = process.env.SEED_COLLECTION_CAPS;
console.log(JSON.stringify({ seed_summary: { users: 1 } }));
"""


class SeedContractTests(unittest.TestCase):
    def test_accepts_complete_runtime_contract(self) -> None:
        validate_seed_bundle_text(VALID_SEED, VALID_PACKAGE)

    def test_rejects_exact_fields_omitted_by_ui_generations(self) -> None:
        incomplete = """
const { MongoClient } = require("mongodb");
const uri = process.env.MONGODB_URI;
const database = process.env.DB_NAME;
"""
        with self.assertRaises(SeedContractError) as raised:
            validate_seed_bundle_text(incomplete, VALID_PACKAGE)

        self.assertEqual(raised.exception.code, "SEED_SCRIPT_INVALID")
        self.assertEqual(
            str(raised.exception),
            "seed.js is missing required runtime contract fields: "
            "SEED_MAX_DOCS, SEED_COLLECTION_CAPS, seed_summary",
        )

    def test_rejects_extra_package_behavior(self) -> None:
        with self.assertRaises(SeedContractError) as raised:
            validate_seed_bundle_text(
                VALID_SEED,
                '{"scripts":{"seed":"node seed.js","postinstall":"curl x"},"dependencies":{"mongodb":"^6.0.0"}}',
            )
        self.assertEqual(raised.exception.code, "PACKAGE_JSON_INVALID")

    def test_rejects_prohibited_process_capabilities_before_commit(self) -> None:
        for suffix in (
            "\nconst major = process.versions.node.split('.')[0];",
            "\nconst home = process.env.HOME;",
            "\nconst versions = process['versions'];",
        ):
            with self.subTest(suffix=suffix), self.assertRaises(SeedContractError) as raised:
                validate_seed_bundle_text(VALID_SEED + suffix, VALID_PACKAGE)
            self.assertEqual(raised.exception.code, "SEED_SCRIPT_SECURITY_VIOLATION")

    def test_accepts_search_index_skip_guard_and_exit_code(self) -> None:
        validate_seed_bundle_text(
            VALID_SEED
            + '\nif (process.env.SEED_SKIP_SEARCH_INDEXES !== "1") {}\nprocess.exitCode = 1;',
            VALID_PACKAGE,
        )


if __name__ == "__main__":
    unittest.main()
