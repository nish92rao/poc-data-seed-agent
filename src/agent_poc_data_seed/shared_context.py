"""Safe shared-state GitHub references and deterministic input normalization."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_VERSION_PATTERN = re.compile(r"^v(\d{3})$")
DEFAULT_SEED_COUNT = 20
DEFAULT_VECTOR_DIMENSIONS = 8


@dataclass(frozen=True)
class GitHubArtifactReference:
    repository: str
    branch: str
    path: str
    commit_sha: str
    url: str


def parse_github_artifact_reference(
    value: Mapping[str, Any], *, expected_repository: str, expected_path: str
) -> GitHubArtifactReference:
    """Validate one shared-state GitHub blob reference without branch ambiguity."""
    path = value.get("path")
    commit_sha = value.get("commit_sha")
    url = value.get("url")
    if path != expected_path or not isinstance(commit_sha, str) or not _SHA_PATTERN.fullmatch(commit_sha):
        raise ValueError(f"{expected_path} reference has invalid path or commit_sha")
    if not isinstance(url, str):
        raise ValueError(f"{expected_path} reference requires a GitHub URL")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment or parsed.username:
        raise ValueError(f"{expected_path} reference URL must be a canonical GitHub HTTPS blob URL")
    segments = parsed.path.lstrip("/").split("/")
    if len(segments) < 5 or segments[2] != "blob":
        raise ValueError(f"{expected_path} reference URL must use GitHub blob format")
    repository = f"{segments[0]}/{segments[1]}"
    if repository != expected_repository:
        raise ValueError("Shared-state GitHub repository does not match GITHUB_REPO")
    decoded = unquote("/".join(segments[3:]))
    suffix = f"/{expected_path}"
    if not decoded.endswith(suffix):
        raise ValueError(f"GitHub URL does not end with {expected_path}")
    branch = decoded[: -len(suffix)]
    if "%" in branch or not branch or any(part in {"", ".", ".."} for part in branch.split("/")):
        raise ValueError("Shared-state GitHub branch is invalid")
    return GitHubArtifactReference(repository, branch, path, commit_sha, url)


def normalize_data_model(value: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Convert Draft Agent data_model.json into the seed validator's canonical model."""
    collections = value.get("collections")
    if not isinstance(collections, list) or not collections:
        raise ValueError("data_model.collections must be a non-empty array")
    names = [collection.get("name") for collection in collections if isinstance(collection, Mapping)]
    if len(names) != len(collections) or any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
        raise ValueError("data_model collection names must be present and unique")
    normalized = []
    defaults = []
    for collection in collections:
        assert isinstance(collection, Mapping)
        shape = collection.get("document_shape")
        if not isinstance(shape, Mapping) or not shape:
            raise ValueError(f"Collection {collection['name']} requires document_shape")
        fields = [_normalize_field(name, definition) for name, definition in shape.items()]
        relationships = _normalize_relationships(collection, set(names))
        indexes = collection.get("indexes")
        if indexes is None:
            indexes = [{"keys": {item["field"]: 1}, "unique": False} for item in relationships]
            defaults.append(f"{collection['name']}: derived relationship indexes")
        seed = collection.get("seed")
        if not isinstance(seed, Mapping) or not isinstance(seed.get("count"), int):
            seed = {"count": DEFAULT_SEED_COUNT, "notes": "deterministic POV default"}
            defaults.append(f"{collection['name']}: seed count {DEFAULT_SEED_COUNT}")
        normalized.append({
            "name": collection["name"],
            "description": collection.get("description", ""),
            "fields": fields,
            "indexes": list(indexes),
            "relationships": relationships,
            "seed": dict(seed),
        })
    return {
        "database_name": str(value.get("database_name") or "poc_seed"),
        "collections": normalized,
        "seed_requirements": dict(value.get("seed_requirements") or {"deterministic_seed": 42}),
    }, defaults


def normalize_query_patterns(value: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Convert keyed Draft Agent query patterns into canonical validator patterns."""
    patterns = []
    defaults = []
    for key, raw in value.items():
        if not isinstance(raw, Mapping) or not isinstance(raw.get("query"), Mapping):
            raise ValueError(f"Query pattern {key} must contain a query object")
        query = raw["query"]
        operation = query.get("operation")
        collection = query.get("collection")
        if operation not in {"find", "aggregate"} or not isinstance(collection, str):
            raise ValueError(f"Query pattern {key} has an invalid operation or collection")
        pattern: dict[str, Any] = {
            "id": str(raw.get("id") or key),
            "name": str(raw.get("name") or key),
            "collection": collection,
            "operation": operation,
        }
        if operation == "aggregate":
            pipeline = query.get("pipeline")
            if not isinstance(pipeline, list):
                raise ValueError(f"Query pattern {key} requires a pipeline")
            pattern["pipeline"] = pipeline
            if any(isinstance(stage, Mapping) and "$vectorSearch" in stage for stage in pipeline):
                pattern["validation_mode"] = "static_vector"
                pattern["vector_dimensions"] = DEFAULT_VECTOR_DIMENSIONS
                defaults.append(f"{key}: synthetic vector dimensions {DEFAULT_VECTOR_DIMENSIONS}")
        else:
            pattern["match"] = dict(query.get("filter") or {})
            for field in ("projection", "sort", "limit"):
                if field in query:
                    pattern[field] = query[field]
        patterns.append(pattern)
    if not patterns:
        raise ValueError("query_patterns must contain at least one pattern")
    return {"patterns": patterns}, defaults


def apply_query_indexes(data_model: Mapping[str, Any], query_patterns: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Derive stable single-field indexes from ordinary match, sort, and lookup usage."""
    normalized = deepcopy(dict(data_model))
    collections = {item["name"]: item for item in normalized.get("collections", [])}
    defaults: list[str] = []
    for pattern in query_patterns.get("patterns", []):
        if pattern.get("validation_mode") == "static_vector":
            continue
        collection = collections.get(pattern.get("collection"))
        if not collection:
            continue
        fields: set[str] = set()
        for key in (pattern.get("match") or {}):
            if isinstance(key, str) and not key.startswith("$"):
                _require_known_field(collection, key, pattern)
                fields.add(key)
        for stage in pattern.get("pipeline") or []:
            if not isinstance(stage, Mapping):
                continue
            for operator in ("$match", "$sort"):
                for key in (stage.get(operator) or {}):
                    if isinstance(key, str) and not key.startswith("$"):
                        if operator == "$match":
                            _require_known_field(collection, key, pattern)
                            fields.add(key)
                        elif _known_field(collection, key):
                            fields.add(key)
            lookup = stage.get("$lookup")
            if isinstance(lookup, Mapping):
                local_field = lookup.get("localField")
                foreign_field = lookup.get("foreignField")
                target = collections.get(lookup.get("from"))
                if isinstance(local_field, str):
                    _require_known_field(collection, local_field, pattern)
                    fields.add(local_field)
                if target and isinstance(foreign_field, str):
                    _require_known_field(target, foreign_field, pattern)
                    _append_index(target, foreign_field, defaults)
        for key in pattern.get("sort") or {}:
            if isinstance(key, str) and not key.startswith("$") and _known_field(collection, key):
                fields.add(key)
        for field in sorted(fields):
            _append_index(collection, field, defaults)
    return normalized, defaults


def next_seed_version(paths: list[str]) -> str:
    """Allocate the next unused immutable seed version from repository paths."""
    versions = set()
    for path in paths:
        if not path.startswith("seed/"):
            continue
        segment = path.split("/", 2)[1]
        match = _VERSION_PATTERN.fullmatch(segment)
        if match:
            versions.add(int(match.group(1)))
    next_value = max(versions, default=0) + 1
    if next_value > 999:
        raise ValueError("Seed version space is exhausted")
    return f"v{next_value:03d}"


def _normalize_field(name: Any, definition: Any) -> dict[str, Any]:
    if not isinstance(name, str) or not isinstance(definition, Mapping) or not isinstance(definition.get("type"), str):
        raise ValueError("data_model fields require string names and types")
    return {"name": name, **dict(definition)}


def _normalize_relationships(collection: Mapping[str, Any], collection_names: set[str]) -> list[dict[str, str]]:
    normalized = []
    for relationship in collection.get("relationships") or []:
        if not isinstance(relationship, Mapping):
            raise ValueError(f"Collection {collection['name']} has an invalid relationship")
        field = relationship.get("field")
        target = relationship.get("target_collection")
        if relationship.get("type") == "one-to-many":
            continue
        if not isinstance(field, str) or target not in collection_names:
            raise ValueError(f"Collection {collection['name']} has a contradictory relationship")
        normalized.append({"field": field, "references": f"{target}._id", "embed_or_reference": "reference"})
    return normalized


def _append_index(collection: dict[str, Any], field: str, defaults: list[str]) -> None:
    indexes = collection.setdefault("indexes", [])
    key = {field: 1}
    if any(isinstance(index, Mapping) and index.get("keys") == key for index in indexes):
        return
    indexes.append({"keys": key, "unique": False})
    defaults.append(f"{collection['name']}: derived query index {field}")


def _known_field(collection: Mapping[str, Any], field: str) -> bool:
    root = field.split(".", 1)[0]
    return root in {item.get("name") for item in collection.get("fields", []) if isinstance(item, Mapping)}


def _require_known_field(collection: Mapping[str, Any], field: str, pattern: Mapping[str, Any]) -> None:
    if not _known_field(collection, field):
        raise ValueError(
            f"Query pattern {pattern.get('id', 'unknown')} references unknown field "
            f"{collection.get('name')}.{field}"
        )