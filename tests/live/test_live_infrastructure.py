"""Opt-in acceptance tests for shared GitHub and the deployed validator."""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_poc_data_seed.github_storage import GitHubRepoStore
from agent_poc_data_seed.validator_client import (
    SeedValidatorClient,
    canonical_json,
    request_signature,
)

GOLDEN_ROOT = Path(__file__).parents[1] / "golden" / "subscription_billing" / "v013"
LIVE_ENABLED = os.getenv("RUN_LIVE_TESTS") == "1"


def http_request(url: str, *, body: str | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    request = Request(
        url,
        data=body.encode("utf-8") if body is not None else None,
        headers=headers or {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(request, timeout=29) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


@unittest.skipUnless(LIVE_ENABLED, "set RUN_LIVE_TESTS=1 to use shared infrastructure")
class LiveInfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.provenance = json.loads((GOLDEN_ROOT / "provenance.json").read_text())
        cls.manifest_text = (GOLDEN_ROOT / "seed.manifest.json").read_text()
        cls.manifest = json.loads(cls.manifest_text)
        cls.schema_text = (GOLDEN_ROOT / "schema_design.json").read_text()
        cls.patterns_text = (GOLDEN_ROOT / "query_patterns.json").read_text()
        cls.store = GitHubRepoStore.from_environment()
        cls.validator = SeedValidatorClient.from_environment()
        cls.validator_url = os.environ["SEED_VALIDATOR_URL"].rstrip("/")
        cls.hmac_secret = os.environ["SEED_VALIDATOR_HMAC_SECRET"]
        cls.mongodb_uri = os.environ["SEED_VALIDATION_MONGODB_URI"]

    def test_github_identity_write_round_trip_and_cleanup(self) -> None:
        result = self.store.smoke_test()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["cleanup"], "succeeded")
        self.assertEqual(result["repository"], self.provenance["repository"])

    def test_exact_pinned_github_bundle_matches_golden_fixture(self) -> None:
        poc_id = self.provenance["poc_id"]
        source_sha = self.provenance["source_commit_sha"]
        bundle = self.store.read_seed_bundle(
            poc_id=poc_id,
            spec_version=self.provenance["spec_version"],
            code_version=self.provenance["code_version"],
            commit_sha=source_sha,
        )
        expected_files = {
            f"pocs/{poc_id}/code/v013/seed/{filename}": (GOLDEN_ROOT / filename).read_text()
            for filename in ("seed.js", "package.json", "SEED_README.md", "seed.manifest.json")
        }
        self.assertEqual(bundle.files, expected_files)

        schema = self.store.read_file(poc_id, self.manifest["inputs"]["schema_design"]["key"], source_sha)
        patterns = self.store.read_file(poc_id, self.manifest["inputs"]["query_patterns"]["key"], source_sha)
        self.assertEqual(schema.content, self.schema_text)
        self.assertEqual(patterns.content, self.patterns_text)

    def test_validator_health_authentication_and_removed_route(self) -> None:
        status, payload = http_request(f"{self.validator_url}/health")
        self.assertEqual((status, payload), (200, {"status": "ok"}))

        status, payload = http_request(
            f"{self.validator_url}/v1/validations/direct",
            body="{}",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "UNAUTHORIZED")

        body = canonical_json({})
        timestamp = int(time.time())
        status, _ = http_request(
            f"{self.validator_url}/v1/artifacts/presign",
            body=body,
            headers={
                "content-type": "application/json",
                "x-validator-timestamp": str(timestamp),
                "x-validator-signature": request_signature(self.hmac_secret, timestamp, body),
            },
        )
        self.assertEqual(status, 404)

    def test_validator_hmac_tamper_expiry_and_bounded_replay(self) -> None:
        body = canonical_json({})
        timestamp = int(time.time())
        signature = request_signature(self.hmac_secret, timestamp, body)
        headers = {
            "content-type": "application/json",
            "x-validator-timestamp": str(timestamp),
            "x-validator-signature": signature,
        }

        first_status, _ = http_request(f"{self.validator_url}/v1/validations/direct", body=body, headers=headers)
        replay_status, _ = http_request(f"{self.validator_url}/v1/validations/direct", body=body, headers=headers)
        self.assertNotEqual(first_status, 401)
        self.assertEqual(replay_status, first_status)

        tampered_status, _ = http_request(
            f"{self.validator_url}/v1/validations/direct",
            body=canonical_json({"tampered": True}),
            headers=headers,
        )
        self.assertEqual(tampered_status, 401)

        stale_timestamp = timestamp - 301
        stale_body = canonical_json({})
        stale_status, _ = http_request(
            f"{self.validator_url}/v1/validations/direct",
            body=stale_body,
            headers={
                "content-type": "application/json",
                "x-validator-timestamp": str(stale_timestamp),
                "x-validator-signature": request_signature(self.hmac_secret, stale_timestamp, stale_body),
            },
        )
        self.assertEqual(stale_status, 401)

    def test_signed_direct_validation_of_pinned_bundle_succeeds(self) -> None:
        correlation = self.manifest["correlation"]
        artifact_contents = {
            filename: (GOLDEN_ROOT / filename).read_text()
            for filename in ("seed.js", "package.json", "SEED_README.md")
        }
        result = self.validator.validate_github_bundle(
            poc_id=self.provenance["poc_id"],
            run_id=correlation["run_id"],
            task_id=correlation["task_id"],
            trace_id=correlation["trace_id"],
            spec_version=self.provenance["spec_version"],
            code_version=self.provenance["code_version"],
            repository=self.provenance["repository"],
            branch=self.provenance["branch"],
            source_commit_sha=self.provenance["source_commit_sha"],
            manifest_json=self.manifest_text,
            schema_design_json=self.schema_text,
            query_patterns_json=self.patterns_text,
            artifacts=artifact_contents,
            mongodb_uri=self.mongodb_uri,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["source_commit_sha"], self.provenance["source_commit_sha"])
        self.assertEqual(result["caps"], {
            "accounts": 3,
            "plans": 1,
            "subscriptions": 6,
            "invoices": 12,
            "payments": 9,
        })
        self.assertEqual(result["seed_summary"], result["caps"])


if __name__ == "__main__":
    unittest.main()
