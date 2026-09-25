"""MongoDB access for the shared POC document."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

from agent_poc_data_seed.shared_context import GitHubArtifactReference, parse_github_artifact_reference


class SharedStateError(RuntimeError):
    """Sanitized shared-state failure."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True)
class SharedPocContext:
    pov_id: str
    data_model: GitHubArtifactReference
    prior_seed: dict[str, str] | None

    @property
    def branch(self) -> str:
        return self.data_model.branch


_client: MongoClient | None = None


def shared_state_collection() -> Collection:
    """Return a pooled collection configured only through shared-state settings."""
    global _client
    uri = os.getenv("SHARED_STATE_MONGODB_URI", "").strip()
    database = os.getenv("SHARED_STATE_DATABASE", "").strip()
    collection = os.getenv("SHARED_STATE_COLLECTION", "").strip()
    if not uri or not database or not collection:
        raise SharedStateError(
            "SHARED_STATE_UNAVAILABLE",
            "Shared POC state is not configured.",
            retryable=True,
        )
    if _client is None:
        _client = MongoClient(
            uri,
            appname="poc-data-seed-shared-state",
            minPoolSize=0,
            connectTimeoutMS=5_000,
            serverSelectionTimeoutMS=5_000,
            socketTimeoutMS=10_000,
        )
    return _client[database][collection]


def load_shared_poc(
    pov_id: str,
    *,
    repository: str | None = None,
    collection: Collection | Any | None = None,
) -> SharedPocContext:
    """Load one POC by exact ``pov_id`` and validate owned artifact references."""
    expected_repository = (repository or os.getenv("GITHUB_REPO", "")).strip()
    if not expected_repository:
        raise SharedStateError("SHARED_STATE_INVALID", "GITHUB_REPO is not configured.", retryable=False)
    source = collection if collection is not None else shared_state_collection()
    try:
        documents = list(source.find({"pov_id": pov_id}, {"pov_id": 1, "spec_artifacts": 1}).limit(2))
    except PyMongoError as error:
        raise SharedStateError(
            "SHARED_STATE_UNAVAILABLE",
            "Shared POC state is temporarily unavailable.",
            retryable=True,
        ) from error
    if not documents:
        raise SharedStateError(
            "POC_NOT_FOUND",
            "No POC was found for the supplied poc_id.",
            retryable=False,
        )
    if len(documents) != 1:
        raise SharedStateError(
            "SHARED_STATE_INVALID",
            "Multiple POC documents use the supplied poc_id.",
            retryable=False,
        )
    document = documents[0]
    artifacts = document.get("spec_artifacts")
    if not isinstance(artifacts, Mapping):
        raise SharedStateError("SHARED_STATE_INVALID", "POC spec_artifacts is missing.", retryable=False)
    try:
        data_model = parse_github_artifact_reference(
            _mapping(artifacts.get("data_model"), "data_model"),
            expected_repository=expected_repository,
            expected_path="spec_architect/data_model.json",
        )
    except ValueError as error:
        raise SharedStateError("SHARED_STATE_INVALID", str(error), retryable=False) from error
    prior_seed = artifacts.get("seed")
    if prior_seed is not None:
        try:
            prior_seed = dict(_mapping(prior_seed, "seed"))
        except ValueError as error:
            raise SharedStateError("SHARED_STATE_INVALID", str(error), retryable=False) from error
    return SharedPocContext(pov_id, data_model, prior_seed)


def publish_seed_pointer(
    context: SharedPocContext,
    seed: Mapping[str, str],
    *,
    collection: Collection | Any | None = None,
) -> None:
    """Publish one validated seed pointer with a strict source-and-pointer CAS."""
    expected_keys = {"path", "commit_sha", "url"}
    if set(seed) != expected_keys or not all(isinstance(seed[key], str) and seed[key] for key in expected_keys):
        raise ValueError("seed pointer must contain non-empty path, commit_sha, and url")
    source = collection if collection is not None else shared_state_collection()
    filter_document: dict[str, Any] = {
        "pov_id": context.pov_id,
        "spec_artifacts.data_model": _reference_document(context.data_model),
    }
    if context.prior_seed is None:
        filter_document["spec_artifacts.seed"] = {"$exists": False}
    else:
        filter_document["spec_artifacts.seed"] = context.prior_seed
    pointer = dict(seed)
    try:
        result = source.update_one(
            filter_document,
            {"$set": {"spec_artifacts.seed": pointer, "updated_at": datetime.now(timezone.utc).isoformat()}},
        )
        if result.modified_count != 1:
            raise SharedStateError(
                "SHARED_STATE_CONFLICT",
                "POC state changed before the seed result could be published.",
                retryable=True,
            )
        found = source.find_one({"pov_id": context.pov_id}, {"spec_artifacts.seed": 1})
    except SharedStateError:
        raise
    except PyMongoError as error:
        raise SharedStateError(
            "SHARED_STATE_UNAVAILABLE",
            "Shared POC state is temporarily unavailable.",
            retryable=True,
        ) from error
    if not isinstance(found, Mapping) or found.get("spec_artifacts", {}).get("seed") != pointer:
        raise SharedStateError(
            "SHARED_STATE_CONFLICT",
            "Published seed state could not be verified.",
            retryable=True,
        )


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"POC spec_artifacts.{name} must be an object")
    return value


def _reference_document(reference: GitHubArtifactReference) -> dict[str, str]:
    return {"path": reference.path, "commit_sha": reference.commit_sha, "url": reference.url}