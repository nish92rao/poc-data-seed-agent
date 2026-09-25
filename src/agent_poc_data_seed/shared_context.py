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
_FIELD_TYPE_ALIASES = {
    "objectid": "objectId",
    "string": "string",
    "bool": "boolean",
    "boolean": "boolean",
    "int": "int",
    "integer": "int",
    "long": "long",
    "double": "double",
    "float": "double",
    "number": "number",
    "decimal": "decimal",
    "date": "date",
    "datetime": "date",
    "object": "document",
    "document": "document",
    "mixed": "mixed",
}


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
        field_array = collection.get("fields")
        if shape is not None and field_array is not None:
            raise ValueError(f"Collection {collection['name']} cannot declare both document_shape and fields")
        if isinstance(shape, Mapping) and shape:
            fields = [_normalize_field(name, definition) for name, definition in shape.items()]
        elif isinstance(field_array, list) and field_array:
            fields = [_normalize_array_field(field) for field in field_array]
        else:
            raise ValueError(f"Collection {collection['name']} requires document_shape or fields")
        _require_unique_fields(fields, str(collection["name"]))
        # Relationship metadata is intentionally non-authoritative for seed generation.
        relationships: list[dict[str, str]] = []
        indexes = _normalize_indexes(collection.get("indexes"))
        if indexes is None:
            indexes = []
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
    """Legacy compatibility helper; production seed workflows bypass query patterns."""
    patterns = []
    defaults = []
    for key, raw in value.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"Query pattern {key} must be an object")
        query = raw.get("query")
        sketch = raw.get("query_sketch")
        if isinstance(query, Mapping) == isinstance(sketch, Mapping):
            raise ValueError(f"Query pattern {key} must contain exactly one query or query_sketch object")
        if isinstance(query, Mapping):
            operation = query.get("operation")
            collection = query.get("collection")
            payload = query
        else:
            assert isinstance(sketch, Mapping)
            collection = sketch.get("collection")
            operation_keys = [
                name for name in ("aggregate", "aggregate_merge", "find", "findOne", "insertOne")
                if name in sketch
            ]
            if len(operation_keys) != 1:
                raise ValueError(f"Query pattern {key} query_sketch requires exactly one supported operation")
            operation = operation_keys[0]
            payload = sketch
        if operation not in {"find", "findOne", "aggregate", "aggregate_merge", "insertOne"} or not isinstance(collection, str):
            raise ValueError(f"Query pattern {key} has an invalid operation or collection")
        pattern: dict[str, Any] = {
            "id": str(raw.get("id") or key),
            "name": str(raw.get("name") or key),
            "collection": collection,
            "operation": operation,
        }
        for metadata_key in ("purpose", "serves", "collections"):
            if metadata_key in raw:
                pattern[metadata_key] = deepcopy(raw[metadata_key])
        if operation in {"aggregate", "aggregate_merge"}:
            pipeline = payload.get("pipeline") if isinstance(query, Mapping) else payload.get(operation)
            if not isinstance(pipeline, list):
                raise ValueError(f"Query pattern {key} requires a pipeline")
            merge_positions = [index for index, stage in enumerate(pipeline) if isinstance(stage, Mapping) and "$merge" in stage]
            if any(isinstance(stage, Mapping) and "$out" in stage for stage in pipeline):
                raise ValueError(f"Query pattern {key} contains a prohibited write stage")
            if operation == "aggregate" and merge_positions:
                raise ValueError(f"Query pattern {key} contains a prohibited write stage")
            if operation == "aggregate_merge" and merge_positions != [len(pipeline) - 1]:
                raise ValueError(f"Query pattern {key} requires one terminal $merge stage")
            pattern["pipeline"] = pipeline
            if any(isinstance(stage, Mapping) and "$vectorSearch" in stage for stage in pipeline):
                pattern["validation_mode"] = "static_vector"
                pattern["vector_dimensions"] = DEFAULT_VECTOR_DIMENSIONS
                defaults.append(f"{key}: synthetic vector dimensions {DEFAULT_VECTOR_DIMENSIONS}")
            elif any(isinstance(stage, Mapping) and "$search" in stage for stage in pipeline):
                pattern["validation_mode"] = "static_search"
        elif operation in {"find", "findOne"}:
            read_payload = payload if isinstance(query, Mapping) else payload[operation]
            if not isinstance(read_payload, Mapping):
                raise ValueError(f"Query pattern {key} requires a {operation} object")
            pattern["match"] = dict(read_payload.get("filter") or {})
            for field in ("projection", "sort", "limit"):
                if field in read_payload:
                    pattern[field] = deepcopy(read_payload[field])
        else:
            document = payload.get("document") if isinstance(query, Mapping) else payload.get("insertOne")
            if not isinstance(document, Mapping) or not document:
                raise ValueError(f"Query pattern {key} requires an insertOne document")
            pattern["document"] = deepcopy(dict(document))
        patterns.append(pattern)
    if not patterns:
        raise ValueError("query_patterns must contain at least one pattern")
    return {"patterns": patterns}, defaults


def apply_query_indexes(data_model: Mapping[str, Any], query_patterns: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Legacy compatibility helper; production validates only data-model indexes."""
    normalized = deepcopy(dict(data_model))
    collections = {item["name"]: item for item in normalized.get("collections", [])}
    defaults: list[str] = []
    for pattern in query_patterns.get("patterns", []):
        collection = collections.get(pattern.get("collection"))
        if not collection:
            raise ValueError(f"Query pattern {pattern.get('id', 'unknown')} references unknown collection")
        if pattern.get("operation") == "insertOne":
            for field in pattern.get("document") or {}:
                _require_known_field(collection, field, pattern)
            continue
        if pattern.get("operation") == "aggregate_merge":
            merge = (pattern.get("pipeline") or [])[-1].get("$merge")
            target_name = merge if isinstance(merge, str) else merge.get("into") if isinstance(merge, Mapping) else None
            if not isinstance(target_name, str) or target_name not in collections:
                raise ValueError(
                    f"Query pattern {pattern.get('id', 'unknown')} references unknown merge collection"
                )
        if pattern.get("validation_mode") == "static_vector":
            vector = next(
                (stage.get("$vectorSearch") for stage in pattern.get("pipeline") or []
                 if isinstance(stage, Mapping) and isinstance(stage.get("$vectorSearch"), Mapping)),
                None,
            )
            if not isinstance(vector, Mapping) or not isinstance(vector.get("path"), str):
                raise ValueError(f"Query pattern {pattern.get('id', 'unknown')} has an invalid vector search")
            _require_known_field(collection, vector["path"], pattern)
            for field in vector.get("filter") or {}:
                if isinstance(field, str) and not field.startswith("$"):
                    _require_known_field(collection, field, pattern)
            continue
        if pattern.get("validation_mode") == "static_search":
            search = next(
                (stage.get("$search") for stage in pattern.get("pipeline") or []
                 if isinstance(stage, Mapping) and isinstance(stage.get("$search"), Mapping)),
                None,
            )
            if not isinstance(search, Mapping) or not isinstance(search.get("index"), str):
                raise ValueError(f"Query pattern {pattern.get('id', 'unknown')} has an invalid search contract")
            continue
        fields: set[str] = set()
        joined_aliases: set[str] = set()
        if pattern.get("operation") in {"find", "findOne"}:
            for key in pattern.get("projection") or {}:
                if isinstance(key, str) and not key.startswith("$"):
                    _require_known_field(collection, key, pattern)
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
                            if key.split(".", 1)[0] not in joined_aliases:
                                _require_known_field(collection, key, pattern)
                                fields.add(key)
                        elif _known_field(collection, key):
                            fields.add(key)
            lookup = stage.get("$lookup")
            if isinstance(lookup, Mapping):
                local_field = lookup.get("localField")
                foreign_field = lookup.get("foreignField")
                target = collections.get(lookup.get("from"))
                if target is None:
                    raise ValueError(
                        f"Query pattern {pattern.get('id', 'unknown')} references unknown lookup collection"
                    )
                if isinstance(local_field, str):
                    _require_known_field(collection, local_field, pattern)
                    fields.add(local_field)
                alias = lookup.get("as")
                if isinstance(alias, str) and alias:
                    joined_aliases.add(alias.split(".", 1)[0])
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
    required = definition.get("required", False)
    if not isinstance(required, bool):
        raise ValueError(f"Field {name} required must be boolean")
    normalized = {"name": name, **dict(definition), "type": _normalize_field_type(definition["type"]), "required": required}
    nested = definition.get("fields")
    if isinstance(nested, Mapping):
        normalized["fields"] = [_normalize_field(child_name, child) for child_name, child in nested.items()]
    elif nested is not None:
        if not isinstance(nested, list):
            raise ValueError(f"Field {name} has invalid nested fields")
        normalized["fields"] = [_normalize_array_field(child) for child in nested]
    return normalized


def _normalize_field_type(value: str) -> str:
    semantic = value.strip()
    if not semantic:
        raise ValueError("data_model field types must be non-empty")
    lower = semantic.lower().replace("_", "-")
    if lower.startswith("array<") and lower.endswith(">"):
        element = semantic[semantic.find("<") + 1:-1]
        return f"array<{_normalize_field_type(element)}>"
    canonical = _FIELD_TYPE_ALIASES.get(lower.replace("-", "")) or _FIELD_TYPE_ALIASES.get(lower)
    if canonical is None:
        raise ValueError(f"Unsupported data_model field type: {value}")
    return canonical


def _normalize_array_field(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("data_model fields must be objects")
    name = value.get("name")
    return _normalize_field(name, {key: deepcopy(item) for key, item in value.items() if key != "name"})


def _require_unique_fields(fields: list[dict[str, Any]], context: str) -> None:
    names = [field["name"] for field in fields]
    if len(set(names)) != len(names):
        raise ValueError(f"{context} has duplicate field names")
    for field in fields:
        nested = field.get("fields")
        if isinstance(nested, list):
            _require_unique_fields(nested, f"{context}.{field['name']}")


def _normalize_indexes(value: Any) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("data_model indexes must be an array")
    normalized = []
    for index in value:
        if not isinstance(index, Mapping):
            raise ValueError("data_model indexes must be objects")
        if isinstance(index.get("keys"), Mapping) and index["keys"]:
            normalized.append({"keys": dict(index["keys"]), "unique": bool(index.get("unique"))})
            continue
        fields = index.get("fields")
        kind = index.get("kind", "regular")
        if kind in {"search", "vector"}:
            continue
        if not isinstance(fields, list) or not fields or not all(isinstance(field, str) and field for field in fields):
            raise ValueError("data_model regular indexes require fields")
        normalized.append({"keys": {field: 1 for field in fields}, "unique": bool(index.get("unique"))})
    return normalized


def _append_index(collection: dict[str, Any], field: str, defaults: list[str]) -> None:
    indexes = collection.setdefault("indexes", [])
    key = {field: 1}
    if any(isinstance(index, Mapping) and index.get("keys") == key for index in indexes):
        return
    indexes.append({"keys": key, "unique": False})
    defaults.append(f"{collection['name']}: derived query index {field}")


def _known_field(collection: Mapping[str, Any], field: str) -> bool:
    return _field_path_exists(collection.get("fields", []), field)


def _field_path_exists(fields: Any, path: str) -> bool:
    if not isinstance(fields, list) or not isinstance(path, str) or not path:
        return False
    parts = path.replace("[]", "").split(".")
    current = fields
    for index, part in enumerate(parts):
        match = next((field for field in current if isinstance(field, Mapping) and field.get("name") == part), None)
        if match is None:
            # Legacy models may declare a dotted path as one field name.
            remaining = ".".join(parts[index:])
            return any(isinstance(field, Mapping) and field.get("name") == remaining for field in current)
        nested = match.get("fields")
        if index < len(parts) - 1 and nested is None and match.get("type") in {"object", "document", "array<object>"}:
            return True
        current = nested if isinstance(nested, list) else []
    return True


def _require_known_field(collection: Mapping[str, Any], field: str, pattern: Mapping[str, Any]) -> None:
    if not _known_field(collection, field):
        raise ValueError(
            f"Query pattern {pattern.get('id', 'unknown')} references unknown field "
            f"{collection.get('name')}.{field}"
        )