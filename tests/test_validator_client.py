"""Unit tests for the signed seed validator HTTP client."""

from __future__ import annotations

import json
import unittest

from agent_poc_data_seed.validator_client import (
    HttpResponse,
    SeedValidatorClient,
    ValidatorClientError,
    request_signature,
)


class ValidatorClientTests(unittest.TestCase):
    secret = "a" * 64

    def test_validator_error_is_structured_without_request_secrets(self) -> None:
        def transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> HttpResponse:
            return HttpResponse(422, '{"status":"failed","error":{"code":"VALIDATION_TIMEOUT","message":"Seed exceeded budget","retryable":true}}')

        client = SeedValidatorClient("https://validator.example.com", self.secret, transport=transport)
        with self.assertRaises(ValidatorClientError) as raised:
            client._post("/v1/validations/direct", {"mongodb_uri": "mongodb+srv://secret@example.com"})

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.payload["error"]["code"], "VALIDATION_TIMEOUT")
        self.assertNotIn("mongodb+srv", str(raised.exception))

    def test_preserves_actionable_seed_script_finding(self) -> None:
        finding = "seed.js is missing required runtime contract fields: SEED_MAX_DOCS, SEED_COLLECTION_CAPS, seed_summary"

        def transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> HttpResponse:
            return HttpResponse(422, json.dumps({
                "status": "failed",
                "error": {
                    "code": "SEED_SCRIPT_INVALID",
                    "message": finding,
                    "failure_class": "IMPLEMENTATION_FAILURE",
                    "retryable": False,
                },
            }))

        client = SeedValidatorClient("https://validator.example.com", self.secret, transport=transport)
        with self.assertRaises(ValidatorClientError) as raised:
            client._post("/v1/validations/direct", {"probe": True})

        self.assertEqual(raised.exception.payload["error"]["message"], finding)

    def test_rejects_insecure_or_invalid_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            SeedValidatorClient("http://validator.example.com", self.secret)
        with self.assertRaisesRegex(ValueError, "less than 29"):
            SeedValidatorClient("https://validator.example.com", self.secret, timeout_seconds=29)
        with self.assertRaisesRegex(ValueError, "at least 32"):
            SeedValidatorClient("https://validator.example.com", "short")

    def test_invalid_json_response_is_sanitized(self) -> None:
        client = SeedValidatorClient(
            "https://validator.example.com",
            self.secret,
            transport=lambda *_: HttpResponse(200, "not-json"),
        )
        with self.assertRaisesRegex(ValidatorClientError, "invalid response"):
            client._post("/v1/validations/direct", {"run_id": "run_1"})

    def test_direct_validation_sends_exact_github_commit_contents(self) -> None:
        captured: dict[str, object] = {}

        def transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> HttpResponse:
            captured.update(url=url, payload=json.loads(body), headers=headers)
            return HttpResponse(200, '{"status":"succeeded","source_commit_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')

        client = SeedValidatorClient("https://validator.example.com", self.secret, transport=transport, clock=lambda: 1_700_000_000)
        result = client.validate_github_bundle(
            poc_id="poc_1",
            run_id="run_1",
            task_id="task_1",
            trace_id="trace_1",
            spec_version="v001",
            code_version="v001",
            repository="owner/repo",
            branch="poc/poc_1",
            source_commit_sha="a" * 40,
            manifest_json="{}",
            schema_design_json='{"collections":[]}',
            artifacts={"seed.js": "seed", "package.json": "{}", "SEED_README.md": "readme"},
            mongodb_uri="mongodb+srv://runtime-only.example.com",
        )

        self.assertEqual(captured["url"], "https://validator.example.com/v1/validations/direct")
        self.assertEqual(captured["payload"]["source_commit_sha"], "a" * 40)
        self.assertEqual(captured["payload"]["artifacts"]["seed.js"], "seed")
        self.assertNotIn("query_patterns_json", captured["payload"])
        self.assertNotIn("authorization", captured["headers"])
        expected_body = json.dumps(captured["payload"], separators=(",", ":"), sort_keys=True)
        self.assertEqual(
            captured["headers"]["x-validator-signature"],
            request_signature(self.secret, 1_700_000_000, expected_body),
        )
        self.assertEqual(result["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()