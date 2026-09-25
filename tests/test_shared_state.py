"""Tests for shared POC lookup and success-only seed publication."""

from __future__ import annotations

import unittest

from agent_poc_data_seed.shared_state import SharedStateError, load_shared_poc, publish_seed_pointer


class Cursor(list):
    def limit(self, count: int):
        return Cursor(self[:count])


class Result:
    def __init__(self, modified_count: int) -> None:
        self.modified_count = modified_count


class FakeCollection:
    def __init__(self, documents: list[dict], *, modified_count: int = 1, readback: dict | None = None) -> None:
        self.documents = documents
        self.modified_count = modified_count
        self.readback = readback
        self.last_filter = None
        self.last_update = None

    def find(self, query, projection):
        return Cursor(self.documents)

    def update_one(self, query, update):
        self.last_filter = query
        self.last_update = update
        return Result(self.modified_count)

    def find_one(self, query, projection):
        if self.readback is not None:
            return self.readback
        return {"spec_artifacts": {"seed": self.last_update["$set"]["spec_artifacts.seed"]}}


def poc_document(*, seed=None, query_patterns=None) -> dict:
    artifacts = {
        "data_model": {
            "path": "spec_architect/data_model.json",
            "commit_sha": "a" * 40,
            "url": "https://github.com/owner/repo/blob/owner/branch/spec_architect/data_model.json",
        },
    }
    if query_patterns is not None:
        artifacts["query_patterns"] = query_patterns
    if seed is not None:
        artifacts["seed"] = seed
    return {"pov_id": "123", "spec_artifacts": artifacts}


class SharedStateTests(unittest.TestCase):
    def test_loads_only_owned_context_by_exact_pov_id(self) -> None:
        context = load_shared_poc("123", repository="owner/repo", collection=FakeCollection([poc_document()]))
        self.assertEqual(context.pov_id, "123")
        self.assertEqual(context.branch, "owner/branch")
        self.assertIsNone(context.prior_seed)

    def test_not_found_duplicate_and_invalid_state_are_distinct(self) -> None:
        cases = (
            (FakeCollection([]), "POC_NOT_FOUND", False),
            (FakeCollection([poc_document(), poc_document()]), "SHARED_STATE_INVALID", False),
            (FakeCollection([{"pov_id": "123"}]), "SHARED_STATE_INVALID", False),
        )
        for collection, code, retryable in cases:
            with self.subTest(code=code), self.assertRaises(SharedStateError) as raised:
                load_shared_poc("123", repository="owner/repo", collection=collection)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(raised.exception.retryable, retryable)

    def test_ignores_missing_or_malformed_query_patterns(self) -> None:
        for query_patterns in (None, "invalid", {"url": "https://example.test"}):
            with self.subTest(query_patterns=query_patterns):
                context = load_shared_poc(
                    "123",
                    repository="owner/repo",
                    collection=FakeCollection([poc_document(query_patterns=query_patterns)]),
                )
                self.assertEqual(context.branch, "owner/branch")

    def test_publish_uses_exact_source_and_absent_seed_cas(self) -> None:
        collection = FakeCollection([poc_document()])
        context = load_shared_poc("123", repository="owner/repo", collection=collection)
        pointer = {
            "path": "seed/v001/seed.js",
            "commit_sha": "c" * 40,
            "url": "https://github.com/owner/repo/blob/owner/branch/seed/v001/seed.js",
        }
        publish_seed_pointer(context, pointer, collection=collection)
        self.assertEqual(collection.last_filter["pov_id"], "123")
        self.assertNotIn("spec_artifacts.query_patterns", collection.last_filter)
        self.assertEqual(collection.last_filter["spec_artifacts.seed"], {"$exists": False})
        self.assertEqual(collection.last_update["$set"]["spec_artifacts.seed"], pointer)
        self.assertIn("updated_at", collection.last_update["$set"])

    def test_publish_compares_prior_seed_and_reports_conflict(self) -> None:
        prior = {"path": "seed/v001/seed.js", "commit_sha": "c" * 40, "url": "https://example.test"}
        collection = FakeCollection([poc_document(seed=prior)], modified_count=0)
        context = load_shared_poc("123", repository="owner/repo", collection=collection)
        with self.assertRaises(SharedStateError) as raised:
            publish_seed_pointer(
                context,
                {"path": "seed/v002/seed.js", "commit_sha": "d" * 40, "url": "https://example.test/new"},
                collection=collection,
            )
        self.assertEqual(raised.exception.code, "SHARED_STATE_CONFLICT")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(collection.last_filter["spec_artifacts.seed"], prior)

    def test_publish_rejects_bad_pointer_and_readback_mismatch(self) -> None:
        context = load_shared_poc("123", repository="owner/repo", collection=FakeCollection([poc_document()]))
        with self.assertRaises(ValueError):
            publish_seed_pointer(context, {"path": "seed.js"})
        collection = FakeCollection([poc_document()], readback={"spec_artifacts": {"seed": {"path": "other"}}})
        with self.assertRaises(SharedStateError) as raised:
            publish_seed_pointer(
                context,
                {"path": "seed/v001/seed.js", "commit_sha": "c" * 40, "url": "https://example.test"},
                collection=collection,
            )
        self.assertEqual(raised.exception.code, "SHARED_STATE_CONFLICT")


if __name__ == "__main__":
    unittest.main()
