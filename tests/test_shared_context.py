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
        self.assertEqual(tickets["relationships"], [])
        self.assertGreaterEqual(len(defaults), 3)

    def test_normalizes_real_query_patterns_and_classifies_vector_search(self) -> None:
        source = json.loads((ROOT / "resources/query_patterns.json").read_text())
        normalized, defaults = normalize_query_patterns(source)
        self.assertEqual(len(normalized["patterns"]), 8)
        vector = next(pattern for pattern in normalized["patterns"] if pattern["id"] == "QP-2")
        self.assertEqual(vector["validation_mode"], "static_vector")
        self.assertEqual(vector["vector_dimensions"], DEFAULT_VECTOR_DIMENSIONS)
        self.assertEqual(len(defaults), 1)

    def test_classifies_atlas_search_for_static_validation(self) -> None:
        normalized, defaults = normalize_query_patterns({"QP-search": {"query": {
            "collection": "chunks",
            "operation": "aggregate",
            "pipeline": [{"$search": {
                "index": "search_chunks_text",
                "text": {"path": "text", "query": "<queryText>"},
            }}],
        }}})
        self.assertEqual(normalized["patterns"][0]["validation_mode"], "static_search")
        self.assertEqual(defaults, [])

    def test_normalizes_fields_array_nested_fields_and_logical_relationships(self) -> None:
        normalized, defaults = normalize_data_model({
            "collections": [
                {
                    "name": "documents",
                    "fields": [
                        {"name": "_id", "type": "objectId", "required": True},
                        {"name": "content", "type": "string", "required": True},
                        {"name": "content_embedding", "type": "array<float>", "required": False},
                        {
                            "name": "metadata",
                            "type": "object",
                            "embedded": True,
                            "fields": [
                                {"name": "tags", "type": "array<string>", "required": False},
                                {"name": "language", "type": "string", "required": False},
                            ],
                        },
                    ],
                    "relationships": [],
                    "indexes": [
                        {"name": "content_regular", "kind": "regular", "fields": ["content"]},
                        {"name": "embedding_vector", "kind": "vector", "fields": ["content_embedding"]},
                    ],
                },
                {
                    "name": "search_queries",
                    "fields": [
                        {"name": "_id", "type": "objectId", "required": True},
                        {
                            "name": "results",
                            "type": "array<object>",
                            "embedded": True,
                            "fields": [
                                {"name": "document_id", "type": "objectId", "required": True},
                                {"name": "score", "type": "double", "required": True},
                            ],
                        },
                    ],
                    "relationships": [{
                        "related_collection": "documents",
                        "type": "many-to-many_logical",
                        "via": "results.document_id",
                    }],
                },
            ],
        })
        documents = normalized["collections"][0]
        metadata = next(field for field in documents["fields"] if field["name"] == "metadata")
        self.assertEqual([field["name"] for field in metadata["fields"]], ["tags", "language"])
        self.assertEqual(documents["indexes"], [{"keys": {"content": 1}, "unique": False}])
        self.assertEqual(normalized["collections"][1]["relationships"], [])
        self.assertEqual(len(defaults), 2)

    def test_normalizes_direct_inverse_and_self_reference_relationships(self) -> None:
        normalized, _ = normalize_data_model({
            "collections": [
                {
                    "name": "teams",
                    "fields": [{"name": "_id", "type": "objectId"}],
                    "relationships": [{"type": "referenced_by", "collection": "tickets", "field": "team_id"}],
                },
                {
                    "name": "tickets",
                    "fields": [
                        {"name": "_id", "type": "objectId"},
                        {"name": "team_id", "type": "objectId"},
                        {"name": "similar", "type": "array<object>", "fields": [
                            {"name": "ticket_id", "type": "objectId"},
                        ]},
                    ],
                    "relationships": [
                        {"type": "references", "collection": "teams", "field": "team_id"},
                        {"type": "self_reference", "field": "similar.ticket_id"},
                    ],
                },
            ],
        })
        teams, tickets = normalized["collections"]
        self.assertEqual(teams["relationships"], [])
        self.assertEqual(tickets["relationships"], [])

    def test_normalizes_to_via_relationships_and_skips_external_many_to_many(self) -> None:
        normalized, _ = normalize_data_model({"collections": [
            {
                "name": "tenants",
                "fields": [{"name": "_id", "type": "objectId"}],
                "relationships": [{"type": "one-to-many", "to": "documents", "via": "documents.tenant_id"}],
            },
            {
                "name": "documents",
                "fields": [{"name": "_id", "type": "objectId"}, {"name": "tenant_id", "type": "objectId"}],
                "relationships": [{"type": "many-to-one", "to": "tenants", "via": "tenant_id"}],
            },
            {
                "name": "chunks",
                "fields": [{"name": "_id", "type": "objectId"}],
                "relationships": [{"type": "many-to-many", "to": "documents", "via": "query_results.chunk_ids"}],
            },
        ]})
        self.assertEqual(normalized["collections"][0]["relationships"], [])
        self.assertEqual(normalized["collections"][1]["relationships"], [])
        self.assertEqual(normalized["collections"][2]["relationships"], [])

    def test_ignores_arbitrary_relationship_dialects(self) -> None:
        normalized, _ = normalize_data_model({"collections": [{
            "name": "customers",
            "document_shape": {"_id": {"type": "objectId"}, "customer_id": {"type": "string"}},
            "relationships": [
                {"type": "one_to_many", "to_collection": "requests", "via": "customer_id"},
                {"type": "anything-at-all", "via": "policy_id, policy_version"},
                "malformed",
            ],
        }]})
        self.assertEqual(normalized["collections"][0]["relationships"], [])

    def test_canonicalizes_field_types_and_validates_required_flags(self) -> None:
        normalized, _ = normalize_data_model({"collections": [{
            "name": "values",
            "fields": [
                {"name": "id", "type": "ObjectId", "required": True},
                {"name": "enabled", "type": "bool"},
                {"name": "count", "type": "integer"},
                {"name": "payload", "type": "object"},
                {"name": "items", "type": "array<document>"},
                {"name": "anything", "type": "mixed"},
            ],
        }]})
        fields = normalized["collections"][0]["fields"]
        self.assertEqual([field["type"] for field in fields], [
            "objectId", "boolean", "int", "document", "array<document>", "mixed",
        ])
        self.assertEqual([field["required"] for field in fields], [True, False, False, False, False, False])
        with self.assertRaisesRegex(ValueError, "required must be boolean"):
            normalize_data_model({"collections": [{
                "name": "bad", "fields": [{"name": "value", "type": "string", "required": "yes"}],
            }]})
        with self.assertRaisesRegex(ValueError, "Unsupported data_model field type"):
            normalize_data_model({"collections": [{
                "name": "bad", "fields": [{"name": "value", "type": "imaginary"}],
            }]})

    def test_normalizes_query_sketch_operations_and_rejects_hidden_writes(self) -> None:
        normalized, defaults = normalize_query_patterns({
            "vector": {
                "query_sketch": {
                    "collection": "documents",
                    "aggregate": [{"$vectorSearch": {
                        "index": "documents_embedding_idx",
                        "path": "content_embedding",
                        "queryVector": "<query_embedding>",
                    }}],
                },
            },
            "insert": {
                "query_sketch": {
                    "collection": "search_queries",
                    "insertOne": {"query_text": "<query_text>", "created_at": "<now>"},
                },
            },
            "history": {
                "query_sketch": {
                    "collection": "search_queries",
                    "find": {"filter": {}, "sort": {"created_at": -1}, "limit": "<page_size>"},
                },
            },
            "document": {
                "query_sketch": {
                    "collection": "documents",
                    "findOne": {"filter": {"_id": "<document_id>"}, "projection": {"content": 1}},
                },
            },
        })
        by_id = {pattern["id"]: pattern for pattern in normalized["patterns"]}
        self.assertEqual(by_id["vector"]["operation"], "aggregate")
        self.assertEqual(by_id["vector"]["validation_mode"], "static_vector")
        self.assertEqual(by_id["insert"]["operation"], "insertOne")
        self.assertEqual(by_id["history"]["operation"], "find")
        self.assertEqual(by_id["document"]["operation"], "findOne")
        self.assertEqual(by_id["document"]["match"], {"_id": "<document_id>"})
        self.assertEqual(len(defaults), 1)
        with self.assertRaisesRegex(ValueError, "prohibited write stage"):
            normalize_query_patterns({"bad": {"query_sketch": {
                "collection": "documents",
                "aggregate": [{"$merge": "other"}],
            }}})

    def test_normalizes_explicit_aggregate_merge_and_validates_target(self) -> None:
        patterns, _ = normalize_query_patterns({"refresh": {"query": {
            "collection": "documents",
            "operation": "aggregate_merge",
            "pipeline": [{"$project": {"value": 1}}, {"$merge": {"into": "snapshots"}}],
        }}})
        self.assertEqual(patterns["patterns"][0]["operation"], "aggregate_merge")
        model, _ = normalize_data_model({"collections": [
            {"name": "documents", "fields": [{"name": "_id", "type": "objectId"}, {"name": "value", "type": "int"}]},
            {"name": "snapshots", "fields": [{"name": "_id", "type": "objectId"}, {"name": "value", "type": "int"}]},
        ]})
        indexed, _ = apply_query_indexes(model, patterns)
        self.assertEqual(len(indexed["collections"]), 2)
        with self.assertRaisesRegex(ValueError, "terminal.*merge"):
            normalize_query_patterns({"bad": {"query": {
                "collection": "documents",
                "operation": "aggregate_merge",
                "pipeline": [{"$merge": "snapshots"}, {"$project": {"value": 1}}],
            }}})

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

    def test_accepts_post_lookup_aliases_without_deriving_source_indexes(self) -> None:
        model, _ = normalize_data_model({"collections": [
            {"name": "chunks", "fields": [
                {"name": "_id", "type": "objectId"},
                {"name": "document_id", "type": "objectId"},
            ]},
            {"name": "documents", "fields": [
                {"name": "_id", "type": "objectId"},
                {"name": "ingest_status", "type": "string"},
            ]},
        ]})
        indexed, _ = apply_query_indexes(model, {"patterns": [{
            "id": "QP-lookup",
            "collection": "chunks",
            "operation": "aggregate",
            "pipeline": [
                {"$lookup": {
                    "from": "documents", "localField": "document_id", "foreignField": "_id", "as": "document",
                }},
                {"$match": {"document.ingest_status": "embedded"}},
            ],
        }]})
        keys = [index["keys"] for index in indexed["collections"][0]["indexes"]]
        self.assertIn({"document_id": 1}, keys)
        self.assertNotIn({"document.ingest_status": 1}, keys)

    def test_accepts_dotted_paths_below_open_ended_object_fields(self) -> None:
        model, _ = normalize_data_model({"collections": [{
            "name": "chunks",
            "fields": [
                {"name": "_id", "type": "objectId"},
                {"name": "structured_attributes", "type": "object"},
            ],
        }]})
        indexed, defaults = apply_query_indexes(model, {"patterns": [{
            "id": "QP-open-object",
            "collection": "chunks",
            "operation": "find",
            "match": {"structured_attributes.memory_type": "episodic"},
        }]})
        self.assertIn(
            {"structured_attributes.memory_type": 1},
            [index["keys"] for index in indexed["collections"][0]["indexes"]],
        )
        self.assertTrue(any("structured_attributes.memory_type" in item for item in defaults))

    def test_rejects_invalid_query_shape(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid operation"):
            normalize_query_patterns({"QP-1": {"query": {"collection": "a", "operation": "delete"}}})

    def test_allocates_next_version_and_consumes_orphans(self) -> None:
        self.assertEqual(next_seed_version([]), "v001")
        self.assertEqual(next_seed_version(["seed/v001/seed.js", "seed/v003/seed.js", "other/v999"]), "v004")
        with self.assertRaisesRegex(ValueError, "exhausted"):
            next_seed_version(["seed/v999/seed.js"])


if __name__ == "__main__":
    unittest.main()
