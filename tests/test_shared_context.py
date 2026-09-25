"""Tests for shared-state GitHub references and Draft Agent input normalization."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent_poc_data_seed.shared_context import (
    DEFAULT_SEED_COUNT,
    DEFAULT_VECTOR_DIMENSIONS,
    apply_query_indexes,
    next_seed_version,
    normalize_data_model,
    normalize_query_patterns,
    parse_github_artifact_reference,
)

ROOT = Path(__file__).parents[1]


class SharedContextTests(unittest.TestCase):
    def test_parses_slash_containing_branch_from_explicit_path_suffix(self) -> None:
        reference = parse_github_artifact_reference(
            {
                "path": "spec_architect/data_model.json",
                "commit_sha": "a" * 40,
                "url": "https://github.com/nish92rao/magenta-test-repo/blob/nishit-rao-mongodb-com/triage-support/spec_architect/data_model.json",
            },
            expected_repository="nish92rao/magenta-test-repo",
            expected_path="spec_architect/data_model.json",
        )
        self.assertEqual(reference.branch, "nishit-rao-mongodb-com/triage-support")

    def test_rejects_cross_repository_wrong_path_and_noncanonical_urls(self) -> None:
        base = {
            "path": "spec_architect/data_model.json",
            "commit_sha": "a" * 40,
            "url": "https://github.com/nish92rao/magenta-test-repo/blob/branch/spec_architect/data_model.json",
        }
        cases = (
            {**base, "url": base["url"].replace("nish92rao", "other")},
            {**base, "path": "other/data_model.json"},
            {**base, "url": base["url"] + "?raw=1"},
            {**base, "commit_sha": "invalid"},
            {**base, "url": base["url"].replace("blob/branch/", "blob/main%252F..%252Fbranch/")},
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_github_artifact_reference(
                    value,
                    expected_repository="nish92rao/magenta-test-repo",
                    expected_path="spec_architect/data_model.json",
                )

    def test_normalizes_real_data_model_with_documented_defaults(self) -> None:
        source = json.loads((ROOT / "resources/data_model.json").read_text())
        normalized, defaults = normalize_data_model(source)
        self.assertEqual(len(normalized["collections"]), 3)
        self.assertTrue(all(collection["seed"]["count"] == DEFAULT_SEED_COUNT for collection in normalized["collections"]))
        tickets = next(item for item in normalized["collections"] if item["name"] == "support_tickets")
        self.assertIn("text_embedding", {field["name"] for field in tickets["fields"]})
        self.assertIn("assigned_team_id", {next(iter(index["keys"])) for index in tickets["indexes"]})
        self.assertGreaterEqual(len(defaults), 3)

    def test_normalizes_real_query_patterns_and_classifies_vector_search(self) -> None:
        source = json.loads((ROOT / "resources/query_patterns.json").read_text())
        normalized, defaults = normalize_query_patterns(source)
        self.assertEqual(len(normalized["patterns"]), 8)
        vector = next(pattern for pattern in normalized["patterns"] if pattern["id"] == "QP-2")
        self.assertEqual(vector["validation_mode"], "static_vector")
        self.assertEqual(vector["vector_dimensions"], DEFAULT_VECTOR_DIMENSIONS)
        self.assertEqual(len(defaults), 1)

    def test_derives_ordinary_query_indexes_but_not_vector_indexes(self) -> None:
        model, _ = normalize_data_model(json.loads((ROOT / "resources/data_model.json").read_text()))
        patterns, _ = normalize_query_patterns(json.loads((ROOT / "resources/query_patterns.json").read_text()))
        indexed, defaults = apply_query_indexes(model, patterns)
        tickets = next(item for item in indexed["collections"] if item["name"] == "support_tickets")
        keys = [index["keys"] for index in tickets["indexes"]]
        self.assertIn({"ticket_id": 1}, keys)
        self.assertIn({"created_at": 1}, keys)
        self.assertFalse(any("text_embedding" in key for key in keys))
        self.assertFalse(any("time_bucket" in key or "priority" in key or "team_name" in key for key in keys))
        self.assertTrue(any("derived query index" in item for item in defaults))

    def test_rejects_query_filters_and_lookups_on_unknown_fields(self) -> None:
        model, _ = normalize_data_model({
            "collections": [{"name": "users", "document_shape": {"_id": {"type": "objectId"}}}],
        })
        with self.assertRaisesRegex(ValueError, "unknown field users.missing"):
            apply_query_indexes(model, {"patterns": [{
                "id": "QP-bad", "collection": "users", "operation": "find", "match": {"missing": "value"},
            }]})

    def test_rejects_contradictory_relationship_and_query_shape(self) -> None:
        with self.assertRaisesRegex(ValueError, "contradictory relationship"):
            normalize_data_model({
                "collections": [{
                    "name": "a",
                    "document_shape": {"_id": {"type": "objectId"}},
                    "relationships": [{"type": "many-to-one", "field": "missing", "target_collection": "b"}],
                }],
            })
        with self.assertRaisesRegex(ValueError, "invalid operation"):
            normalize_query_patterns({"QP-1": {"query": {"collection": "a", "operation": "delete"}}})

    def test_allocates_next_version_and_consumes_orphans(self) -> None:
        self.assertEqual(next_seed_version([]), "v001")
        self.assertEqual(next_seed_version(["seed/v001/seed.js", "seed/v003/seed.js", "other/v999"]), "v004")
        with self.assertRaisesRegex(ValueError, "exhausted"):
            next_seed_version(["seed/v999/seed.js"])


if __name__ == "__main__":
    unittest.main()
