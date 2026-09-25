"""Fast deterministic checks applied before an immutable seed bundle commit."""

from __future__ import annotations

import json
import re


class SeedContractError(ValueError):
    """A generated seed artifact violates the production contract."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def validate_seed_bundle_text(seed_js: str, package_json: str) -> None:
    """Reject common contract violations before writing an immutable version."""
    try:
        package = json.loads(package_json)
    except json.JSONDecodeError as error:
        raise SeedContractError("PACKAGE_JSON_INVALID", "package.json is not valid JSON") from error
    if not isinstance(package, dict):
        raise SeedContractError("PACKAGE_JSON_INVALID", "package.json must be an object")
    if package.get("scripts") != {"seed": "node seed.js"} or set(package.get("dependencies", {})) != {"mongodb"}:
        raise SeedContractError(
            "PACKAGE_JSON_INVALID",
            "package.json must declare only mongodb and scripts.seed must equal node seed.js",
        )
    if any(package.get(field) for field in ("devDependencies", "optionalDependencies", "peerDependencies", "bin")):
        raise SeedContractError("PACKAGE_JSON_INVALID", "package.json contains unsupported executable or dependency fields")

    required = {
        "mongodb driver import": re.compile(r"require\([\"'](?:mongodb|node:mongodb)[\"']\)"),
        "MONGODB_URI": re.compile(r"process\.env\.MONGODB_URI"),
        "DB_NAME": re.compile(r"process\.env\.DB_NAME"),
        "SEED_MAX_DOCS": re.compile(r"process\.env\.SEED_MAX_DOCS"),
        "SEED_COLLECTION_CAPS": re.compile(r"process\.env\.SEED_COLLECTION_CAPS"),
        "seed_summary": re.compile(r"seed_summary"),
    }
    missing = [name for name, pattern in required.items() if not pattern.search(seed_js)]
    if missing:
        raise SeedContractError(
            "SEED_SCRIPT_INVALID",
            f"seed.js is missing required runtime contract fields: {', '.join(missing)}",
        )

    if re.search(r"\beval\s*\(|\bnew\s+Function\s*\(|\bFunction\s*\(|\bimport\s*\(", seed_js):
        raise SeedContractError("SEED_SCRIPT_SECURITY_VIOLATION", "seed.js uses prohibited dynamic execution")
    allowed_modules = {"mongodb", "crypto", "node:crypto"}
    imported_modules = re.findall(r"require\([\"']([^\"']+)[\"']\)", seed_js)
    if any(module not in allowed_modules for module in imported_modules):
        raise SeedContractError("SEED_SCRIPT_SECURITY_VIOLATION", "seed.js imports a prohibited module")
    allowed_process_access = re.compile(
        r"process\.(?:env\.(?:MONGODB_URI|DB_NAME|SEED_MAX_DOCS|SEED_COLLECTION_CAPS|SEED_SKIP_SEARCH_INDEXES)|exitCode)\b"
    )
    without_allowed_process_access = allowed_process_access.sub("", seed_js)
    if re.search(r"\bprocess\s*(?:\.|\[)", without_allowed_process_access):
        raise SeedContractError(
            "SEED_SCRIPT_SECURITY_VIOLATION",
            "seed.js accesses a prohibited process capability or environment variable",
        )
