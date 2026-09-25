"""Bootstrap the shared-state sample POC's GitHub specification artifacts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pymongo.errors import PyMongoError

from agent_poc_data_seed.github_storage import GitHubRepoStore
from agent_poc_data_seed.shared_state import SharedStateError, load_shared_poc, shared_state_collection


_DATA_MODEL_PATH = "spec_architect/data_model.json"
_QUERY_PATTERNS_PATH = "spec_architect/query_patterns.json"


def bootstrap_shared_fixture(
    pov_id: str,
    *,
    workspace: Path,
    store: GitHubRepoStore | None = None,
    collection: Any | None = None,
) -> dict[str, str]:
    """Publish fixture inputs and atomically refresh their shared-state references."""
    repository = os.getenv("GITHUB_REPO", "").strip()
    if not repository:
        raise SharedStateError("SHARED_STATE_INVALID", "GITHUB_REPO is not configured.", retryable=False)
    source = collection if collection is not None else shared_state_collection()
    try:
        source.create_index("pov_id", unique=True, name="pov_id_unique")
    except PyMongoError as error:
        raise SharedStateError(
            "SHARED_STATE_INVALID",
            "Shared POC state cannot enforce unique pov_id values.",
            retryable=False,
        ) from error
    context = load_shared_poc(pov_id, repository=repository, collection=source)
    github = store if store is not None else GitHubRepoStore.from_environment()
    branch_info = github.resolve_or_create_branch(context.branch)

    local_files = {
        _DATA_MODEL_PATH: workspace.joinpath("resources", "data_model.json").read_text(encoding="utf-8"),
        _QUERY_PATTERNS_PATH: workspace.joinpath("resources", "query_patterns.json").read_text(encoding="utf-8"),
    }
    before = {path: hashlib.sha256(content.encode("utf-8")).hexdigest() for path, content in local_files.items()}
    committed = github.commit_project_files_atomic(
        context.branch,
        local_files,
        f"spec({pov_id}): bootstrap seed inputs",
        expected_head_sha=branch_info["head_sha"],
    )
    for path, expected_hash in before.items():
        artifact = github.read_project_file(path, committed.commit_sha)
        if artifact.sha256 != expected_hash:
            raise SharedStateError("GITHUB_CONTENT_MISMATCH", "Bootstrapped GitHub input failed read-back.", retryable=False)

    branch_url = quote(context.branch, safe="/")
    updated = {
        "data_model": {
            "path": _DATA_MODEL_PATH,
            "commit_sha": committed.commit_sha,
            "url": f"https://github.com/{repository}/blob/{branch_url}/{_DATA_MODEL_PATH}",
        },
        "query_patterns": {
            "path": _QUERY_PATTERNS_PATH,
            "commit_sha": committed.commit_sha,
            "url": f"https://github.com/{repository}/blob/{branch_url}/{_QUERY_PATTERNS_PATH}",
        },
    }
    filter_document = {
        "pov_id": pov_id,
        "spec_artifacts.data_model": {
            "path": context.data_model.path,
            "commit_sha": context.data_model.commit_sha,
            "url": context.data_model.url,
        },
        "spec_artifacts.query_patterns": {
            "path": context.query_patterns.path,
            "commit_sha": context.query_patterns.commit_sha,
            "url": context.query_patterns.url,
        },
    }
    try:
        result = source.update_one(filter_document, {"$set": {
            "spec_artifacts.data_model": updated["data_model"],
            "spec_artifacts.query_patterns": updated["query_patterns"],
        }})
        if result.matched_count != 1:
            raise SharedStateError(
                "SHARED_STATE_CONFLICT",
                "POC inputs changed before bootstrap could publish their references.",
                retryable=True,
            )
        found = source.find_one({"pov_id": pov_id}, {
            "spec_artifacts.data_model": 1,
            "spec_artifacts.query_patterns": 1,
        })
    except SharedStateError:
        raise
    except PyMongoError as error:
        raise SharedStateError(
            "SHARED_STATE_UNAVAILABLE",
            "Shared POC state is temporarily unavailable.",
            retryable=True,
        ) from error
    artifacts = found.get("spec_artifacts", {}) if isinstance(found, dict) else {}
    if artifacts.get("data_model") != updated["data_model"] or artifacts.get("query_patterns") != updated["query_patterns"]:
        raise SharedStateError("SHARED_STATE_CONFLICT", "Bootstrapped shared state failed read-back.", retryable=True)
    for path, expected_hash in before.items():
        current = workspace.joinpath("resources", Path(path).name).read_bytes()
        if hashlib.sha256(current).hexdigest() != expected_hash:
            raise RuntimeError("Local bootstrap fixture changed during publication")
    return {
        "pov_id": pov_id,
        "repository": repository,
        "branch": context.branch,
        "commit_sha": committed.commit_sha,
        "data_model_path": _DATA_MODEL_PATH,
        "query_patterns_path": _QUERY_PATTERNS_PATH,
    }
