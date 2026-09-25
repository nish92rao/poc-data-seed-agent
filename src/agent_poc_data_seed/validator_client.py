"""Signed HTTP client for the external seed validator."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agent_poc_data_seed.failures import public_error
DEFAULT_TIMEOUT_SECONDS = 24.0


@dataclass(frozen=True)
class HttpResponse:
    """Minimal HTTP response abstraction used by the validator client."""

    status_code: int
    body: str


class ValidatorClientError(RuntimeError):
    """A sanitized validator API failure."""

    def __init__(self, status_code: int | None, payload: Mapping[str, Any]) -> None:
        self.status_code = status_code
        source = dict(payload)
        source_error = source.get("error")
        source_error = dict(source_error) if isinstance(source_error, Mapping) else {}
        code = source_error.get("code")
        if not code:
            code = "UNAUTHORIZED" if status_code == 401 else "VALIDATOR_INFRASTRUCTURE_FAILURE"
        normalized = public_error(code, source_error.get("message"))
        if isinstance(source_error.get("failure_class"), str):
            normalized["failure_class"] = source_error["failure_class"]
        self.payload = {"status": "failed", "error": normalized}
        report_key = source.get("report_key")
        if isinstance(report_key, str) and report_key.startswith("pocs/") and ".." not in report_key:
            self.payload["report_key"] = report_key
        super().__init__(normalized["message"])


HttpTransport = Callable[[str, bytes, Mapping[str, str], float], HttpResponse]


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Serialize a request deterministically so the signed bytes are unambiguous."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def request_signature(secret: str, timestamp: int, body: str) -> str:
    """Create the HMAC expected by the validator Lambda."""
    return hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.{body}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _default_transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> HttpResponse:
    request = Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return HttpResponse(response.status, response.read().decode("utf-8"))
    except HTTPError as error:
        return HttpResponse(error.code, error.read().decode("utf-8"))
    except URLError as error:
        raise ValidatorClientError(None, {"error": {"code": "VALIDATOR_INFRASTRUCTURE_FAILURE"}}) from error


class SeedValidatorClient:
    """Calls the public validator API without exposing credentials to the LLM."""

    def __init__(
        self,
        base_url: str,
        hmac_secret: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: HttpTransport = _default_transport,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("SEED_VALIDATOR_URL must use HTTPS.")
        if len(hmac_secret) < 32:
            raise ValueError("SEED_VALIDATOR_HMAC_SECRET must be at least 32 characters.")
        if not 0 < timeout_seconds < 29:
            raise ValueError("Validator timeout must be greater than 0 and less than 29 seconds.")
        self._base_url = base_url.rstrip("/")
        self._hmac_secret = hmac_secret
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._clock = clock

    @classmethod
    def from_environment(cls) -> "SeedValidatorClient":
        base_url = os.getenv("SEED_VALIDATOR_URL", "")
        hmac_secret = os.getenv("SEED_VALIDATOR_HMAC_SECRET", "")
        timeout_seconds = float(os.getenv("SEED_VALIDATOR_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
        return cls(base_url, hmac_secret, timeout_seconds=timeout_seconds)

    def validate_github_bundle(
        self,
        *,
        poc_id: str,
        run_id: str,
        task_id: str,
        trace_id: str,
        spec_version: str,
        code_version: str,
        repository: str,
        branch: str,
        source_commit_sha: str,
        manifest_json: str,
        schema_design_json: str = "",
        data_model_json: str = "",
        normalized_data_model_json: str = "",
        artifacts: Mapping[str, str],
        mongodb_uri: str,
    ) -> dict[str, Any]:
        """Validate exact contents reread from an immutable GitHub commit."""
        return self._post(
            "/v1/validations/direct",
            {
                "poc_id": poc_id,
                "run_id": run_id,
                "task_id": task_id,
                "trace_id": trace_id,
                "spec_version": spec_version,
                "code_version": code_version,
                "repository": repository,
                "branch": branch,
                "source_commit_sha": source_commit_sha,
                "manifest_json": manifest_json,
                **({"schema_design_json": schema_design_json} if schema_design_json else {}),
                **({"data_model_json": data_model_json} if data_model_json else {}),
                **({"normalized_data_model_json": normalized_data_model_json} if normalized_data_model_json else {}),
                "artifacts": dict(artifacts),
                "mongodb_uri": mongodb_uri,
            },
        )

    def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = canonical_json(payload)
        timestamp = int(self._clock())
        headers = {
            "content-type": "application/json",
            "x-validator-timestamp": str(timestamp),
            "x-validator-signature": request_signature(self._hmac_secret, timestamp, body),
        }
        response = self._transport(
            f"{self._base_url}{path}",
            body.encode("utf-8"),
            headers,
            self._timeout_seconds,
        )
        try:
            parsed = json.loads(response.body)
        except json.JSONDecodeError as error:
            raise ValidatorClientError(response.status_code, {"error": {"code": "VALIDATOR_INVALID_RESPONSE"}}) from error
        if not isinstance(parsed, dict):
            raise ValidatorClientError(response.status_code, {"error": {"code": "VALIDATOR_INVALID_RESPONSE"}})
        if not 200 <= response.status_code < 300:
            raise ValidatorClientError(response.status_code, parsed)
        return parsed