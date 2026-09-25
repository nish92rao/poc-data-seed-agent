"""Tests for GitHub configuration and isolated smoke operations."""

from __future__ import annotations

import base64
import json
import unittest

from agent_poc_data_seed.github_storage import (
    GitHubConfig,
    GitHubRepoStore,
    GitHubStoreError,
    HttpResponse,
    build_project_seed_bundle_files,
    build_seed_bundle_files,
)


class FakeGitHub:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict | None]] = []
        self.branch = ""
        self.content = b""

    def __call__(self, url: str, method: str, body: bytes | None, headers: dict[str, str], timeout: float) -> HttpResponse:
        path = url.removeprefix("https://api.github.com")
        payload = json.loads(body) if body else None
        self.requests.append((method, path, payload))
        response: dict = {}
        status = 200
        if path == "/user":
            response = {"login": "nish92rao"}
        elif path == "/repos/nish92rao/magenta-test-repo":
            response = {"default_branch": "main"}
        elif path.endswith("/git/ref/heads/main"):
            response = {"object": {"sha": "a" * 40}}
        elif path.endswith("/git/refs") and method == "POST":
            self.branch = payload["ref"].removeprefix("refs/heads/")
            response = {"ref": payload["ref"]}
            status = 201
        elif "/contents/" in path and method == "PUT":
            self.content = base64.b64decode(payload["content"])
            response = {"commit": {"sha": "b" * 40}}
            status = 201
        elif "/contents/" in path and method == "GET":
            response = {"content": base64.b64encode(self.content).decode("ascii")}
        elif "/git/refs/heads/" in path and method == "DELETE":
            response = {}
            status = 204
        else:
            raise AssertionError((method, path, payload))
        return HttpResponse(status, json.dumps(response).encode(), {"x-ratelimit-remaining": "4995"})


class AtomicGitHub:
    def __init__(self, *, existing: dict[str, str] | None = None) -> None:
        self.requests: list[tuple[str, str, dict | None]] = []
        self.existing = existing or {}
        self.head = "a" * 40
        self.blob_index = 0

    def __call__(self, url: str, method: str, body: bytes | None, headers: dict[str, str], timeout: float) -> HttpResponse:
        path = url.removeprefix("https://api.github.com")
        payload = json.loads(body) if body else None
        self.requests.append((method, path, payload))
        status = 200
        response: dict = {}
        if "/git/ref/heads/poc%2F" in path and method == "GET":
            response = {"object": {"sha": self.head}}
        elif "/contents/" in path and method == "GET":
            artifact_path = path.split("/contents/", 1)[1].split("?", 1)[0]
            content = self.existing.get(artifact_path)
            if content is None:
                status = 404
                response = {"message": "Not Found"}
            else:
                response = {"type": "file", "content": base64.b64encode(content.encode()).decode()}
        elif f"/git/commits/{self.head}" in path and method == "GET":
            response = {"tree": {"sha": "b" * 40}}
        elif path.endswith("/git/blobs") and method == "POST":
            self.blob_index += 1
            response = {"sha": format(self.blob_index, "040x")}
            status = 201
        elif path.endswith("/git/trees") and method == "POST":
            response = {"sha": "c" * 40}
            status = 201
        elif path.endswith("/git/commits") and method == "POST":
            response = {"sha": "d" * 40}
            status = 201
        elif "/git/refs/heads/poc%2F" in path and method == "PATCH":
            response = {"object": {"sha": payload["sha"]}}
        else:
            raise AssertionError((method, path, payload))
        return HttpResponse(status, json.dumps(response).encode(), {"x-ratelimit-remaining": "4980"})


class GitHubStorageTests(unittest.TestCase):
    @staticmethod
    def bundle_files(*, repair: bool = False) -> dict[str, str]:
        return build_seed_bundle_files(
            repository="nish92rao/magenta-test-repo",
            poc_id="poc_123",
            spec_version="v001",
            code_version="v002" if repair else "v001",
            seed_js="seed",
            package_json="{}",
            seed_readme="readme",
            schema_input={"key": "pocs/poc_123/spec/v001/schema_design.json", "sha256": "a" * 64},
            correlation={"poc_id": "poc_123", "run_id": "run_1", "task_id": "task_1", "trace_id": "trace_1", "producer": "poc-data-seed"},
            repair_notes="notes" if repair else None,
            repair={"kind": "repair", "previous_code_version": "v001"} if repair else None,
        )

    def test_config_requires_owner_repo_format(self) -> None:
        with self.assertRaisesRegex(ValueError, "owner/repo"):
            GitHubConfig("nish92rao", "x" * 40, "magenta-test-repo")

    def test_smoke_creates_verifies_and_deletes_isolated_branch(self) -> None:
        fake = FakeGitHub()
        store = GitHubRepoStore(GitHubConfig("nish92rao", "x" * 40, "nish92rao/magenta-test-repo"), transport=fake)
        result = store.smoke_test()
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["smoke_branch"].startswith("smoke/poc-data-seed-"))
        self.assertEqual(result["rate_limit_remaining"], 4995)
        self.assertEqual(result["cleanup"], "succeeded")
        self.assertEqual([request[0] for request in fake.requests], ["GET", "GET", "GET", "POST", "PUT", "GET", "DELETE"])
        self.assertIn("/git/refs/heads/", fake.requests[-1][1])

    def test_error_taxonomy_does_not_expose_response_body(self) -> None:
        def unauthorized(url: str, method: str, body: bytes | None, headers: dict[str, str], timeout: float) -> HttpResponse:
            return HttpResponse(401, b'{"message":"token ghp_secret"}', {})

        store = GitHubRepoStore(GitHubConfig("nish92rao", "x" * 40, "nish92rao/magenta-test-repo"), transport=unauthorized)
        with self.assertRaises(GitHubStoreError) as context:
            store.smoke_test()
        self.assertEqual(context.exception.code, "GITHUB_AUTH_FAILED")
        self.assertNotIn("ghp_secret", str(context.exception))

    def test_atomic_commit_uses_one_tree_commit_and_ref_update(self) -> None:
        fake = AtomicGitHub()
        store = GitHubRepoStore(GitHubConfig("nish92rao", "x" * 40, "nish92rao/magenta-test-repo"), transport=fake)
        result = store.commit_files_atomic(
            "poc_123",
            {
                "pocs/poc_123/code/v001/seed/seed.js": "seed",
                "pocs/poc_123/code/v001/seed/package.json": "{}",
            },
            "feat: add seed bundle",
        )
        self.assertEqual(result.branch, "poc/poc_123")
        self.assertEqual(result.commit_sha, "d" * 40)
        self.assertFalse(result.idempotent)
        methods_and_suffixes = [(method, path.rsplit("/", 1)[-1]) for method, path, _ in fake.requests]
        self.assertIn(("POST", "trees"), methods_and_suffixes)
        self.assertIn(("POST", "commits"), methods_and_suffixes)
        self.assertEqual(fake.requests[-1][0], "PATCH")
        self.assertFalse(fake.requests[-1][2]["force"])

    def test_same_content_retry_is_idempotent_without_commit(self) -> None:
        path = "pocs/poc_123/code/v001/seed/seed.js"
        fake = AtomicGitHub(existing={path: "seed"})
        store = GitHubRepoStore(GitHubConfig("nish92rao", "x" * 40, "nish92rao/magenta-test-repo"), transport=fake)
        result = store.commit_files_atomic("poc_123", {path: "seed"}, "feat: add seed", expected_head_sha="e" * 40)
        self.assertTrue(result.idempotent)
        self.assertFalse(any(path.endswith("/git/blobs") for _, path, _ in fake.requests))

    def test_rejects_changed_existing_content_and_stale_head(self) -> None:
        path = "pocs/poc_123/code/v001/seed/seed.js"
        fake = AtomicGitHub(existing={path: "old"})
        store = GitHubRepoStore(GitHubConfig("nish92rao", "x" * 40, "nish92rao/magenta-test-repo"), transport=fake)
        with self.assertRaises(GitHubStoreError) as collision:
            store.commit_files_atomic("poc_123", {path: "new"}, "feat: add seed")
        self.assertEqual(collision.exception.code, "GITHUB_ARTIFACT_EXISTS")
        with self.assertRaises(GitHubStoreError) as conflict:
            store.commit_files_atomic("poc_123", {"pocs/poc_123/code/v002/seed/seed.js": "new"}, "feat: add seed", expected_head_sha="e" * 40)
        self.assertEqual(conflict.exception.code, "GITHUB_CONFLICT")

    def test_rejects_paths_outside_poc_prefix(self) -> None:
        store = GitHubRepoStore(GitHubConfig("nish92rao", "x" * 40, "nish92rao/magenta-test-repo"), transport=AtomicGitHub())
        with self.assertRaisesRegex(ValueError, "canonical POC prefix"):
            store.commit_files_atomic("poc_123", {"pocs/poc_other/seed.js": "bad"}, "bad path")

    def test_rejects_noncanonical_and_traversal_paths(self) -> None:
        rejected = (
            "pocs/poc_123/../poc_other/seed.js",
            "pocs/poc_123//code/v001/seed.js",
            "pocs/poc_123/./code/v001/seed.js",
            "pocs/poc_123/%2e%2e/poc_other/seed.js",
            "pocs/poc_123/code\\v001\\seed.js",
            "pocs/poc_123/code/v001/seed\n.js",
            "pocs/poc_123/codé/v001/seed.js",
        )
        for path in rejected:
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "unsafe"):
                GitHubRepoStore._safe_path(path, "poc_123")

    def test_project_paths_are_confined_to_spec_and_versioned_seed(self) -> None:
        for path in (
            "spec_architect/data_model.json",
            "spec_architect/query_patterns.json",
            "seed/v001/seed.js",
            "seed/v999/validation/run_1/report.json",
        ):
            self.assertEqual(GitHubRepoStore._safe_project_path(path), path)
        for path in ("backend/index.js", "seed/latest/seed.js", "seed/v001/../secret", "spec_architect/api_contract.json"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                GitHubRepoStore._safe_project_path(path)

    def test_builds_top_level_project_bundle_with_only_data_model_input(self) -> None:
        files = build_project_seed_bundle_files(
            repository="owner/repo",
            branch="owner/project",
            poc_id="1790237138344",
            code_version="v001",
            seed_js="seed",
            package_json="{}",
            seed_readme="readme",
            data_model_input={"key": "spec_architect/data_model.json", "sha256": "a" * 64, "commit_sha": "b" * 40},
            correlation={"poc_id": "1790237138344", "run_id": "run_1", "task_id": "task_1", "trace_id": "trace_1", "producer": "poc-data-seed"},
            defaults_applied=["tickets: seed count 20"],
        )
        self.assertEqual(set(files), {
            "seed/v001/seed.js",
            "seed/v001/package.json",
            "seed/v001/SEED_README.md",
            "seed/v001/seed.manifest.json",
        })
        manifest = json.loads(files["seed/v001/seed.manifest.json"])
        self.assertEqual(manifest["storage"]["branch"], "owner/project")
        self.assertEqual(manifest["inputs"]["data_model"]["commit_sha"], "b" * 40)
        self.assertEqual(set(manifest["inputs"]), {"data_model"})
        self.assertEqual(manifest["defaults_applied"], ["tickets: seed count 20"])

    def test_read_bundle_rejects_wrong_storage_and_input_identity(self) -> None:
        files = self.bundle_files()
        manifest_path = "pocs/poc_123/code/v001/seed/seed.manifest.json"
        manifest = json.loads(files[manifest_path])
        cases = (
            ("storage", lambda value: value["storage"].update(repository="other/repo")),
            ("branch", lambda value: value["storage"].update(branch="poc/poc_other")),
            ("input", lambda value: value["inputs"]["schema_design"].update(key="pocs/poc_other/spec/v001/schema_design.json")),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                changed = json.loads(json.dumps(manifest))
                mutate(changed)
                existing = {**files, manifest_path: json.dumps(changed)}
                store = GitHubRepoStore(
                    GitHubConfig("nish92rao", "x" * 40, "nish92rao/magenta-test-repo"),
                    transport=AtomicGitHub(existing=existing),
                )
                with self.assertRaises(GitHubStoreError) as raised:
                    store.read_seed_bundle(
                        poc_id="poc_123",
                        spec_version="v001",
                        code_version="v001",
                        commit_sha="a" * 40,
                    )
                self.assertEqual(raised.exception.code, "GITHUB_MANIFEST_INVALID")

    def test_builds_atomic_seed_bundle_with_self_verifying_manifest(self) -> None:
        files = self.bundle_files()
        manifest_path = "pocs/poc_123/code/v001/seed/seed.manifest.json"
        self.assertEqual(len(files), 4)
        manifest = json.loads(files[manifest_path])
        self.assertEqual(manifest["storage"]["branch"], "poc/poc_123")
        self.assertEqual(manifest["storage"]["repository"], "nish92rao/magenta-test-repo")
        self.assertEqual(len(manifest["artifacts"]), 3)
        by_name = {entry["key"].rsplit("/", 1)[-1]: entry for entry in manifest["artifacts"]}
        self.assertEqual(by_name["seed.js"]["bytes"], 4)

    def test_repair_bundle_requires_notes_and_records_lineage(self) -> None:
        files = self.bundle_files(repair=True)
        manifest = json.loads(files["pocs/poc_123/code/v002/seed/seed.manifest.json"])
        self.assertEqual(manifest["repair"]["previous_code_version"], "v001")
        self.assertIn("pocs/poc_123/code/v002/seed/REPAIR_NOTES.md", files)
        with self.assertRaisesRegex(ValueError, "both repair notes and repair metadata"):
            build_seed_bundle_files(
                repository="nish92rao/magenta-test-repo",
                poc_id="poc_123",
                spec_version="v001",
                code_version="v002",
                seed_js="seed",
                package_json="{}",
                seed_readme="readme",
                schema_input={"key": "pocs/poc_123/spec/v001/schema_design.json", "sha256": "a" * 64},
                correlation={"poc_id": "poc_123", "run_id": "run_1", "task_id": "task_1", "trace_id": "trace_1", "producer": "poc-data-seed"},
                repair_notes="notes",
            )


if __name__ == "__main__":
    unittest.main()