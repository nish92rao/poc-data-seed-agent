"""GitHub repository connectivity and storage primitives."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

GITHUB_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_POC_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
MAX_COMMIT_FILES = 20
MAX_COMMIT_BYTES = 8 * 1024 * 1024
_VERSION_PATTERN = re.compile(r"^v\d{3}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class GitHubStoreError(RuntimeError):
    """Sanitized GitHub API failure."""

    def __init__(self, code: str, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class GitHubConfig:
    username: str
    token: str
    repository: str

    def __post_init__(self) -> None:
        if not self.username or not re.fullmatch(r"[A-Za-z0-9-]+", self.username):
            raise ValueError("GITHUB_USERNAME must be a GitHub username")
        if len(self.token) < 20:
            raise ValueError("GITHUB_TOKEN is not configured")
        if not _REPOSITORY_PATTERN.fullmatch(self.repository) or self.repository.endswith(".git"):
            raise ValueError("GITHUB_REPO must use owner/repo format")

    @classmethod
    def from_environment(cls) -> "GitHubConfig":
        username = os.getenv("GITHUB_USERNAME", "").strip()
        token = os.getenv("GITHUB_TOKEN", "").strip()
        repository = os.getenv("GITHUB_REPO", "").strip()
        return cls(username, token, repository)


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str]


HttpTransport = Callable[[str, str, bytes | None, Mapping[str, str], float], HttpResponse]


@dataclass(frozen=True)
class GitHubArtifact:
    path: str
    content: str
    sha256: str
    bytes: int
    commit_sha: str


@dataclass(frozen=True)
class GitHubCommit:
    repository: str
    branch: str
    commit_sha: str
    artifacts: tuple[dict[str, Any], ...]
    idempotent: bool = False


@dataclass(frozen=True)
class GitHubSeedBundle:
    repository: str
    branch: str
    commit_sha: str
    manifest: dict[str, Any]
    files: dict[str, str]


def _default_transport(
    url: str,
    method: str,
    body: bytes | None,
    headers: Mapping[str, str],
    timeout: float,
) -> HttpResponse:
    request = Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return HttpResponse(response.status, response.read(), dict(response.headers.items()))
    except HTTPError as error:
        return HttpResponse(error.code, error.read(), dict(error.headers.items()))
    except (URLError, OSError) as error:
        raise GitHubStoreError("GITHUB_UNREACHABLE", "GitHub is unreachable.", retryable=True) from error


class GitHubRepoStore:
    """Minimal GitHub REST client used to prove repository write access."""

    def __init__(
        self,
        config: GitHubConfig,
        *,
        transport: HttpTransport = _default_transport,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._config = config
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> "GitHubRepoStore":
        return cls(GitHubConfig.from_environment())

    def smoke_test(self) -> dict[str, Any]:
        """Create, verify, and delete one isolated smoke branch and file."""
        user = self._request("GET", "/user")
        login = str(user.get("login", ""))
        if login.casefold() != self._config.username.casefold():
            raise GitHubStoreError(
                "GITHUB_IDENTITY_MISMATCH",
                "GITHUB_USERNAME does not match the authenticated GitHub user.",
                retryable=False,
            )
        repository = self._request("GET", f"/repos/{self._config.repository}")
        default_branch = str(repository.get("default_branch", ""))
        if not default_branch:
            raise GitHubStoreError("GITHUB_INVALID_RESPONSE", "GitHub repository has no default branch.", retryable=False)
        base_ref = self._request("GET", self._ref_path(default_branch))
        base_sha = str(base_ref.get("object", {}).get("sha", ""))
        if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
            raise GitHubStoreError("GITHUB_INVALID_RESPONSE", "GitHub returned an invalid branch head.", retryable=True)

        suffix = f"{int(time.time())}-{secrets.token_hex(4)}"
        branch = f"smoke/poc-data-seed-{suffix}"
        path = f".smoke/poc-data-seed-{suffix}.txt"
        content = f"poc-data-seed GitHub smoke {suffix}\n".encode("utf-8")
        branch_created = False
        result: dict[str, Any] | None = None
        try:
            self._request("POST", f"/repos/{self._config.repository}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})
            branch_created = True
            committed = self._request(
                "PUT",
                f"/repos/{self._config.repository}/contents/{quote(path, safe='/')}",
                {
                    "message": "test: verify poc-data-seed GitHub connectivity",
                    "content": base64.b64encode(content).decode("ascii"),
                    "branch": branch,
                },
            )
            commit_sha = str(committed.get("commit", {}).get("sha", ""))
            if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
                raise GitHubStoreError("GITHUB_INVALID_RESPONSE", "GitHub returned an invalid commit SHA.", retryable=True)
            read_back = self._request(
                "GET",
                f"/repos/{self._config.repository}/contents/{quote(path, safe='/')}?ref={commit_sha}",
            )
            encoded = str(read_back.get("content", "")).replace("\n", "")
            try:
                returned = base64.b64decode(encoded, validate=True)
            except ValueError as error:
                raise GitHubStoreError("GITHUB_INVALID_RESPONSE", "GitHub returned invalid file content.", retryable=True) from error
            if returned != content:
                raise GitHubStoreError("GITHUB_CONTENT_MISMATCH", "GitHub smoke content did not round-trip.", retryable=False)
            result = {
                "status": "succeeded",
                "repository": self._config.repository,
                "default_branch": default_branch,
                "smoke_branch": branch,
                "path": path,
                "commit_sha": commit_sha,
                "sha256": hashlib.sha256(content).hexdigest(),
                "rate_limit_remaining": self._last_rate_limit_remaining,
            }
        finally:
            if branch_created:
                self._request("DELETE", self._ref_path(branch, plural=True))
        if result is None:
            raise GitHubStoreError("GITHUB_INVALID_RESPONSE", "GitHub smoke test did not produce a result.", retryable=True)
        return {**result, "cleanup": "succeeded"}

    def resolve_poc_branch(self, poc_id: str) -> dict[str, str]:
        """Resolve or create the deterministic branch for one POC."""
        branch = self.branch_for_poc(poc_id)
        try:
            ref = self._request("GET", self._ref_path(branch))
        except GitHubStoreError as error:
            if error.code != "GITHUB_NOT_FOUND":
                raise
            repository = self._request("GET", f"/repos/{self._config.repository}")
            default_branch = str(repository.get("default_branch", ""))
            if not default_branch:
                raise GitHubStoreError("GITHUB_INVALID_RESPONSE", "GitHub repository has no default branch.", retryable=False)
            default_ref = self._request("GET", self._ref_path(default_branch))
            base_sha = self._commit_sha(default_ref.get("object", {}).get("sha"))
            ref = self._request(
                "POST",
                f"/repos/{self._config.repository}/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": base_sha},
            )
        head_sha = self._commit_sha(ref.get("object", {}).get("sha"))
        return {
            "repo_name": self._config.repository,
            "repo_url": f"https://github.com/{self._config.repository}",
            "branch": branch,
            "head_sha": head_sha,
        }

    def resolve_existing_branch(self, branch: str) -> dict[str, str]:
        """Resolve an existing shared-state branch without creating it."""
        safe_branch = self._safe_branch(branch)
        ref = self._request("GET", self._ref_path(safe_branch))
        return {
            "repo_name": self._config.repository,
            "repo_url": f"https://github.com/{self._config.repository}",
            "branch": safe_branch,
            "head_sha": self._commit_sha(ref.get("object", {}).get("sha")),
        }

    def resolve_or_create_branch(self, branch: str) -> dict[str, str]:
        """Resolve an explicit project branch, creating it from the default branch if absent."""
        safe_branch = self._safe_branch(branch)
        try:
            return self.resolve_existing_branch(safe_branch)
        except GitHubStoreError as error:
            if error.code != "GITHUB_NOT_FOUND":
                raise
        repository = self._request("GET", f"/repos/{self._config.repository}")
        default_branch = str(repository.get("default_branch", ""))
        if not default_branch:
            raise GitHubStoreError("GITHUB_INVALID_RESPONSE", "GitHub repository has no default branch.", retryable=False)
        default_ref = self._request("GET", self._ref_path(default_branch))
        base_sha = self._commit_sha(default_ref.get("object", {}).get("sha"))
        self._request("POST", f"/repos/{self._config.repository}/git/refs", {
            "ref": f"refs/heads/{safe_branch}", "sha": base_sha,
        })
        return {
            "repo_name": self._config.repository,
            "repo_url": f"https://github.com/{self._config.repository}",
            "branch": safe_branch,
            "head_sha": base_sha,
        }

    def read_project_file(self, path: str, commit_sha: str) -> GitHubArtifact:
        """Read an allowed project artifact from an exact commit."""
        safe_path = self._safe_project_path(path)
        pinned_sha = self._commit_sha(commit_sha)
        response = self._request("GET", f"/repos/{self._config.repository}/contents/{quote(safe_path, safe='/')}?ref={pinned_sha}")
        encoded = str(response.get("content", "")).replace("\n", "")
        try:
            content = base64.b64decode(encoded, validate=True)
            text = content.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise GitHubStoreError("GITHUB_INVALID_RESPONSE", "GitHub returned invalid UTF-8 file content.", retryable=True) from error
        return GitHubArtifact(safe_path, text, hashlib.sha256(content).hexdigest(), len(content), pinned_sha)

    def list_project_paths(self, commit_sha: str) -> tuple[str, ...]:
        """List allowed project paths recursively from an exact commit tree."""
        pinned_sha = self._commit_sha(commit_sha)
        response = self._request("GET", f"/repos/{self._config.repository}/git/trees/{pinned_sha}?recursive=1")
        if response.get("truncated") is True or not isinstance(response.get("tree"), list):
            raise GitHubStoreError("GITHUB_INVALID_RESPONSE", "GitHub returned an incomplete repository tree.", retryable=True)
        paths = []
        for entry in response["tree"]:
            if isinstance(entry, Mapping) and entry.get("type") == "blob" and isinstance(entry.get("path"), str):
                try:
                    paths.append(self._safe_project_path(entry["path"]))
                except ValueError:
                    continue
        return tuple(sorted(paths))

    def commit_project_files_atomic(
        self,
        branch: str,
        files: Mapping[str, str],
        message: str,
        *,
        expected_head_sha: str,
    ) -> GitHubCommit:
        """Atomically commit files confined to spec_architect or seed/vNNN."""
        safe_branch = self._safe_branch(branch)
        expected = self._commit_sha(expected_head_sha)
        if not files or len(files) > MAX_COMMIT_FILES:
            raise ValueError(f"GitHub commit must contain 1 to {MAX_COMMIT_FILES} files")
        if not all(isinstance(content, str) for content in files.values()):
            raise ValueError("GitHub file contents must be UTF-8 strings")
        normalized = {self._safe_project_path(path): content.encode("utf-8") for path, content in files.items()}
        if sum(len(content) for content in normalized.values()) > MAX_COMMIT_BYTES:
            raise ValueError(f"GitHub commit payload exceeds {MAX_COMMIT_BYTES} bytes")
        head_sha = self.resolve_existing_branch(safe_branch)["head_sha"]
        if head_sha != expected:
            raise GitHubStoreError("GITHUB_CONFLICT", "GitHub branch head changed before commit.", retryable=True, status_code=409)
        existing: dict[str, GitHubArtifact] = {}
        for path in normalized:
            try:
                existing[path] = self.read_project_file(path, head_sha)
            except GitHubStoreError as error:
                if error.code != "GITHUB_NOT_FOUND":
                    raise
        if existing:
            all_same = len(existing) == len(normalized) and all(
                existing[path].content.encode("utf-8") == content for path, content in normalized.items()
            )
            if all_same:
                return GitHubCommit(self._config.repository, safe_branch, head_sha, tuple(
                    self._artifact_record(path, content) for path, content in sorted(normalized.items())
                ), True)
            raise GitHubStoreError("GITHUB_ARTIFACT_EXISTS", "An immutable GitHub artifact path already exists.", retryable=False, status_code=409)
        commit = self._request("GET", f"/repos/{self._config.repository}/git/commits/{head_sha}")
        base_tree = self._commit_sha(commit.get("tree", {}).get("sha"))
        entries = []
        for path, content in sorted(normalized.items()):
            blob = self._request("POST", f"/repos/{self._config.repository}/git/blobs", {
                "content": base64.b64encode(content).decode("ascii"), "encoding": "base64",
            })
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": self._commit_sha(blob.get("sha"))})
        tree = self._request("POST", f"/repos/{self._config.repository}/git/trees", {"base_tree": base_tree, "tree": entries})
        created = self._request("POST", f"/repos/{self._config.repository}/git/commits", {
            "message": message.strip(), "tree": self._commit_sha(tree.get("sha")), "parents": [head_sha],
        })
        commit_sha = self._commit_sha(created.get("sha"))
        self._request("PATCH", self._ref_path(safe_branch, plural=True), {"sha": commit_sha, "force": False})
        return GitHubCommit(self._config.repository, safe_branch, commit_sha, tuple(
            self._artifact_record(path, content) for path, content in sorted(normalized.items())
        ))

    def read_project_seed_bundle(
        self,
        *,
        branch: str,
        poc_id: str,
        code_version: str,
        commit_sha: str,
    ) -> GitHubSeedBundle:
        """Read and verify one top-level seed/vNNN bundle at an exact commit."""
        safe_branch = self._safe_branch(branch)
        self._version(code_version, "code_version")
        prefix = f"seed/{code_version}"
        manifest_path = f"{prefix}/seed.manifest.json"
        manifest_artifact = self.read_project_file(manifest_path, commit_sha)
        try:
            manifest = json.loads(manifest_artifact.content)
        except json.JSONDecodeError as error:
            raise GitHubStoreError("GITHUB_MANIFEST_INVALID", "GitHub seed manifest is invalid JSON.", retryable=False) from error
        storage = manifest.get("storage") if isinstance(manifest, Mapping) else None
        inputs = manifest.get("inputs") if isinstance(manifest, Mapping) else None
        if (
            not isinstance(manifest, Mapping)
            or manifest.get("poc_id") != poc_id
            or manifest.get("code_version") != code_version
            or storage != {"provider": "github", "repository": self._config.repository, "branch": safe_branch}
            or not isinstance(inputs, Mapping)
            or inputs.get("data_model", {}).get("key") != "spec_architect/data_model.json"
        ):
            raise GitHubStoreError("GITHUB_MANIFEST_INVALID", "GitHub seed manifest identity is invalid.", retryable=False)
        expected_names = {"seed.js", "package.json", "SEED_README.md"}
        if manifest.get("repair"):
            expected_names.add("REPAIR_NOTES.md")
        entries = manifest.get("artifacts")
        if not isinstance(entries, list) or len(entries) != len(expected_names):
            raise GitHubStoreError("GITHUB_MANIFEST_INVALID", "GitHub seed manifest artifact set is invalid.", retryable=False)
        files = {manifest_path: manifest_artifact.content}
        seen = set()
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise GitHubStoreError("GITHUB_MANIFEST_INVALID", "GitHub seed manifest artifact entry is invalid.", retryable=False)
            path = str(entry.get("key", ""))
            name = path.rsplit("/", 1)[-1]
            if name not in expected_names or name in seen or path != f"{prefix}/{name}":
                raise GitHubStoreError("GITHUB_MANIFEST_INVALID", "GitHub seed manifest contains a noncanonical artifact.", retryable=False)
            artifact = self.read_project_file(path, commit_sha)
            if entry.get("sha256") != artifact.sha256 or entry.get("bytes") != artifact.bytes:
                raise GitHubStoreError("GITHUB_CONTENT_MISMATCH", "GitHub artifact does not match its manifest.", retryable=False)
            seen.add(name)
            files[path] = artifact.content
        if seen != expected_names:
            raise GitHubStoreError("GITHUB_MANIFEST_INVALID", "GitHub seed manifest is incomplete.", retryable=False)
        return GitHubSeedBundle(self._config.repository, safe_branch, commit_sha, dict(manifest), files)

    def read_file(self, poc_id: str, path: str, commit_sha: str) -> GitHubArtifact:
        """Read one canonical UTF-8 file from an exact immutable commit."""
        self.branch_for_poc(poc_id)
        safe_path = self._safe_path(path, poc_id)
        pinned_sha = self._commit_sha(commit_sha)
        response = self._request(
            "GET",
            f"/repos/{self._config.repository}/contents/{quote(safe_path, safe='/')}?ref={pinned_sha}",
        )
        if response.get("type") not in {None, "file"}:
            raise GitHubStoreError("GITHUB_INVALID_RESPONSE", "GitHub path is not a file.", retryable=False)
        encoded = str(response.get("content", "")).replace("\n", "")
        try:
            content = base64.b64decode(encoded, validate=True)
            text = content.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise GitHubStoreError("GITHUB_INVALID_RESPONSE", "GitHub returned invalid UTF-8 file content.", retryable=True) from error
        return GitHubArtifact(safe_path, text, hashlib.sha256(content).hexdigest(), len(content), pinned_sha)

    def commit_files_atomic(
        self,
        poc_id: str,
        files: Mapping[str, str],
        message: str,
        *,
        expected_head_sha: str | None = None,
    ) -> GitHubCommit:
        """Commit a complete immutable file set atomically on the POC branch."""
        if not isinstance(message, str) or not message.strip() or len(message) > 200:
            raise ValueError("GitHub commit message must contain 1 to 200 characters")
        if not files or len(files) > MAX_COMMIT_FILES:
            raise ValueError(f"GitHub commit must contain 1 to {MAX_COMMIT_FILES} files")
        normalized: dict[str, bytes] = {}
        for path, content in files.items():
            safe_path = self._safe_path(path, poc_id)
            if not isinstance(content, str):
                raise ValueError("GitHub file contents must be UTF-8 strings")
            normalized[safe_path] = content.encode("utf-8")
        if sum(len(content) for content in normalized.values()) > MAX_COMMIT_BYTES:
            raise ValueError(f"GitHub commit payload exceeds {MAX_COMMIT_BYTES} bytes")

        branch_info = self.resolve_poc_branch(poc_id)
        branch = branch_info["branch"]
        head_sha = branch_info["head_sha"]
        existing: dict[str, GitHubArtifact] = {}
        for path in normalized:
            try:
                existing[path] = self.read_file(poc_id, path, head_sha)
            except GitHubStoreError as error:
                if error.code != "GITHUB_NOT_FOUND":
                    raise
        if existing:
            all_same = len(existing) == len(normalized) and all(
                existing[path].content.encode("utf-8") == content for path, content in normalized.items()
            )
            if all_same:
                return GitHubCommit(
                    self._config.repository,
                    branch,
                    head_sha,
                    tuple(self._artifact_record(path, content) for path, content in sorted(normalized.items())),
                    True,
                )
            raise GitHubStoreError("GITHUB_ARTIFACT_EXISTS", "An immutable GitHub artifact path already exists.", retryable=False, status_code=409)
        if expected_head_sha is not None and self._commit_sha(expected_head_sha) != head_sha:
            raise GitHubStoreError("GITHUB_CONFLICT", "GitHub branch head changed before commit.", retryable=True, status_code=409)

        commit = self._request("GET", f"/repos/{self._config.repository}/git/commits/{head_sha}")
        base_tree = str(commit.get("tree", {}).get("sha", ""))
        if not _COMMIT_SHA_PATTERN.fullmatch(base_tree):
            raise GitHubStoreError("GITHUB_INVALID_RESPONSE", "GitHub returned an invalid tree SHA.", retryable=True)
        entries = []
        for path, content in sorted(normalized.items()):
            blob = self._request(
                "POST",
                f"/repos/{self._config.repository}/git/blobs",
                {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
            )
            blob_sha = self._commit_sha(blob.get("sha"))
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})
        tree = self._request(
            "POST",
            f"/repos/{self._config.repository}/git/trees",
            {"base_tree": base_tree, "tree": entries},
        )
        tree_sha = self._commit_sha(tree.get("sha"))
        created = self._request(
            "POST",
            f"/repos/{self._config.repository}/git/commits",
            {"message": message.strip(), "tree": tree_sha, "parents": [head_sha]},
        )
        commit_sha = self._commit_sha(created.get("sha"))
        self._request(
            "PATCH",
            self._ref_path(branch, plural=True),
            {"sha": commit_sha, "force": False},
        )
        return GitHubCommit(
            self._config.repository,
            branch,
            commit_sha,
            tuple(self._artifact_record(path, content) for path, content in sorted(normalized.items())),
        )

    def commit_seed_bundle(
        self,
        *,
        poc_id: str,
        spec_version: str,
        code_version: str,
        seed_js: str,
        package_json: str,
        seed_readme: str,
        schema_input: Mapping[str, str],
        correlation: Mapping[str, str],
        repair_notes: str | None = None,
        repair: Mapping[str, str] | None = None,
        expected_head_sha: str | None = None,
    ) -> GitHubCommit:
        """Build and atomically commit one complete immutable seed bundle."""
        files = build_seed_bundle_files(
            repository=self._config.repository,
            poc_id=poc_id,
            spec_version=spec_version,
            code_version=code_version,
            seed_js=seed_js,
            package_json=package_json,
            seed_readme=seed_readme,
            schema_input=schema_input,
            correlation=correlation,
            repair_notes=repair_notes,
            repair=repair,
        )
        return self.commit_files_atomic(
            poc_id,
            files,
            f"seed({poc_id}): commit {code_version}",
            expected_head_sha=expected_head_sha,
        )

    def read_seed_bundle(
        self,
        *,
        poc_id: str,
        spec_version: str,
        code_version: str,
        commit_sha: str,
    ) -> GitHubSeedBundle:
        """Read and verify one complete seed bundle from an exact commit SHA."""
        self._version(spec_version, "spec_version")
        self._version(code_version, "code_version")
        branch = self.branch_for_poc(poc_id)
        prefix = f"pocs/{poc_id}/code/{code_version}/seed"
        manifest_path = f"{prefix}/seed.manifest.json"
        manifest_artifact = self.read_file(poc_id, manifest_path, commit_sha)
        try:
            manifest = json.loads(manifest_artifact.content)
        except json.JSONDecodeError as error:
            raise GitHubStoreError("GITHUB_MANIFEST_INVALID", "GitHub seed manifest is invalid JSON.", retryable=False) from error
        if (
            not isinstance(manifest, dict)
            or manifest.get("poc_id") != poc_id
            or manifest.get("spec_version") != spec_version
            or manifest.get("code_version") != code_version
        ):
            raise GitHubStoreError("GITHUB_MANIFEST_INVALID", "GitHub seed manifest identity is invalid.", retryable=False)
        storage = manifest.get("storage")
        if not isinstance(storage, Mapping) or storage != {
            "provider": "github",
            "repository": self._config.repository,
            "branch": branch,
        }:
            raise GitHubStoreError("GITHUB_MANIFEST_INVALID", "GitHub seed manifest storage identity is invalid.", retryable=False)
        inputs = manifest.get("inputs")
        if not isinstance(inputs, Mapping):
            raise GitHubStoreError("GITHUB_MANIFEST_INVALID", "GitHub seed manifest inputs are invalid.", retryable=False)
        entry = inputs.get("schema_design")
        if (
            not isinstance(entry, Mapping)
            or entry.get("key") != f"pocs/{poc_id}/spec/{spec_version}/schema_design.json"
            or not isinstance(entry.get("sha256"), str)
            or not _SHA256_PATTERN.fullmatch(entry["sha256"])
        ):
            raise GitHubStoreError("GITHUB_MANIFEST_INVALID", "GitHub seed manifest input identity is invalid.", retryable=False)
        entries = manifest.get("artifacts")
        expected_names = {"seed.js", "package.json", "SEED_README.md"}
        if manifest.get("repair"):
            expected_names.add("REPAIR_NOTES.md")
        if not isinstance(entries, list) or len(entries) != len(expected_names):
            raise GitHubStoreError("GITHUB_MANIFEST_INVALID", "GitHub seed manifest artifact set is invalid.", retryable=False)
        files: dict[str, str] = {manifest_path: manifest_artifact.content}
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise GitHubStoreError("GITHUB_MANIFEST_INVALID", "GitHub seed manifest artifact entry is invalid.", retryable=False)
            path = str(entry.get("key", ""))
            name = path.rsplit("/", 1)[-1]
            if name not in expected_names or name in seen or path != f"{prefix}/{name}":
                raise GitHubStoreError("GITHUB_MANIFEST_INVALID", "GitHub seed manifest contains a noncanonical artifact.", retryable=False)
            artifact = self.read_file(poc_id, path, commit_sha)
            if entry.get("sha256") != artifact.sha256 or entry.get("bytes") != artifact.bytes:
                raise GitHubStoreError("GITHUB_CONTENT_MISMATCH", "GitHub artifact does not match its manifest.", retryable=False)
            seen.add(name)
            files[path] = artifact.content
        if seen != expected_names:
            raise GitHubStoreError("GITHUB_MANIFEST_INVALID", "GitHub seed manifest is incomplete.", retryable=False)
        return GitHubSeedBundle(self._config.repository, branch, commit_sha, manifest, files)

    @staticmethod
    def branch_for_poc(poc_id: str) -> str:
        if not isinstance(poc_id, str) or not _POC_ID_PATTERN.fullmatch(poc_id):
            raise ValueError("poc_id may contain only letters, numbers, underscores, and hyphens")
        return f"poc/{poc_id}"

    @staticmethod
    def _safe_path(path: str, poc_id: str) -> str:
        if not isinstance(path, str) or not path.isascii() or path.startswith(("/", ".")):
            raise ValueError("GitHub artifact path is unsafe")
        segments = path.split("/")
        if any(not segment or segment in {".", ".."} for segment in segments):
            raise ValueError("GitHub artifact path is unsafe")
        if "%" in path or "\\" in path or any(ord(character) < 0x20 or ord(character) == 0x7F for character in path):
            raise ValueError("GitHub artifact path is unsafe")
        prefix = f"pocs/{poc_id}/"
        if not path.startswith(prefix):
            raise ValueError("GitHub artifact path must stay within its canonical POC prefix")
        return path

    @staticmethod
    def _safe_branch(branch: str) -> str:
        if (
            not isinstance(branch, str)
            or not branch.isascii()
            or len(branch) > 200
            or branch.startswith(("/", "."))
            or branch.endswith(("/", "."))
            or ".." in branch
            or "//" in branch
            or "%" in branch
            or "\\" in branch
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in branch)
        ):
            raise ValueError("GitHub branch is unsafe")
        return branch

    @staticmethod
    def _safe_project_path(path: str) -> str:
        if not isinstance(path, str) or not path.isascii() or path.startswith(("/", ".")):
            raise ValueError("GitHub project path is unsafe")
        segments = path.split("/")
        if any(not segment or segment in {".", ".."} for segment in segments):
            raise ValueError("GitHub project path is unsafe")
        if "%" in path or "\\" in path or any(ord(character) < 0x20 or ord(character) == 0x7F for character in path):
            raise ValueError("GitHub project path is unsafe")
        if path in {"spec_architect/data_model.json", "spec_architect/query_patterns.json"}:
            return path
        if len(segments) >= 3 and segments[0] == "seed" and _VERSION_PATTERN.fullmatch(segments[1]):
            return path
        raise ValueError("GitHub project path is outside the allowed spec and seed prefixes")

    @staticmethod
    def _commit_sha(value: Any) -> str:
        if not isinstance(value, str) or not _COMMIT_SHA_PATTERN.fullmatch(value):
            raise GitHubStoreError("GITHUB_INVALID_RESPONSE", "GitHub returned an invalid commit SHA.", retryable=True)
        return value

    @staticmethod
    def _artifact_record(path: str, content: bytes) -> dict[str, Any]:
        return {"key": path, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}

    @staticmethod
    def _version(value: str, name: str) -> str:
        if not isinstance(value, str) or not _VERSION_PATTERN.fullmatch(value):
            raise ValueError(f"{name} must use the vNNN format")
        return value

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "accept": "application/vnd.github+json",
            "authorization": f"Bearer {self._config.token}",
            "content-type": "application/json",
            "user-agent": "poc-data-seed-agent",
            "x-github-api-version": GITHUB_API_VERSION,
        }

    _last_rate_limit_remaining: int | None = None

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        response = self._transport(f"{GITHUB_API_URL}{path}", method, body, self._headers, self._timeout_seconds)
        remaining = response.headers.get("x-ratelimit-remaining") or response.headers.get("X-RateLimit-Remaining")
        self._last_rate_limit_remaining = int(remaining) if isinstance(remaining, str) and remaining.isdigit() else None
        parsed: Any = {}
        if response.body:
            try:
                parsed = json.loads(response.body)
            except json.JSONDecodeError as error:
                raise GitHubStoreError("GITHUB_INVALID_RESPONSE", "GitHub returned invalid JSON.", retryable=True, status_code=response.status_code) from error
        if not 200 <= response.status_code < 300:
            raise self._error(response.status_code)
        if not isinstance(parsed, dict):
            raise GitHubStoreError("GITHUB_INVALID_RESPONSE", "GitHub returned an unexpected response.", retryable=True)
        return parsed

    def _error(self, status_code: int) -> GitHubStoreError:
        if status_code == 401:
            return GitHubStoreError("GITHUB_AUTH_FAILED", "GitHub authentication failed.", retryable=False, status_code=status_code)
        if status_code == 403 and self._last_rate_limit_remaining == 0:
            return GitHubStoreError("GITHUB_RATE_LIMITED", "GitHub rate limit was exceeded.", retryable=True, status_code=status_code)
        if status_code == 404:
            return GitHubStoreError("GITHUB_NOT_FOUND", "GitHub repository, branch, or path was not found.", retryable=False, status_code=status_code)
        if status_code in {409, 422}:
            return GitHubStoreError("GITHUB_CONFLICT", "GitHub rejected a conflicting update.", retryable=True, status_code=status_code)
        return GitHubStoreError("GITHUB_API_ERROR", "GitHub API request failed.", retryable=status_code >= 500, status_code=status_code)

    def _ref_path(self, branch: str, *, plural: bool = False) -> str:
        collection = "refs" if plural else "ref"
        return f"/repos/{self._config.repository}/git/{collection}/heads/{quote(branch, safe='')}"


def build_seed_bundle_files(
    *,
    repository: str,
    poc_id: str,
    spec_version: str,
    code_version: str,
    seed_js: str,
    package_json: str,
    seed_readme: str,
    schema_input: Mapping[str, str],
    correlation: Mapping[str, str],
    repair_notes: str | None = None,
    repair: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build canonical seed files and their self-verifying manifest."""
    GitHubRepoStore.branch_for_poc(poc_id)
    GitHubRepoStore._version(spec_version, "spec_version")
    GitHubRepoStore._version(code_version, "code_version")
    required_correlation = {"poc_id", "run_id", "task_id", "trace_id", "producer"}
    if set(correlation) != required_correlation or correlation.get("poc_id") != poc_id:
        raise ValueError("correlation must contain matching poc_id, run_id, task_id, trace_id, and producer")
    expected_schema_path = f"pocs/{poc_id}/spec/{spec_version}/schema_design.json"
    if schema_input.get("key") != expected_schema_path:
        raise ValueError("seed manifest input must use the canonical schema path")
    if not _SHA256_PATTERN.fullmatch(str(schema_input.get("sha256", ""))):
        raise ValueError("seed manifest input requires a lowercase SHA-256 digest")
    prefix = f"pocs/{poc_id}/code/{code_version}/seed"
    files = {
        f"{prefix}/seed.js": seed_js,
        f"{prefix}/package.json": package_json,
        f"{prefix}/SEED_README.md": seed_readme,
    }
    if repair_notes is not None:
        files[f"{prefix}/REPAIR_NOTES.md"] = repair_notes
    if bool(repair_notes is not None) != bool(repair is not None):
        raise ValueError("repair bundles require both repair notes and repair metadata")
    artifacts = [
        GitHubRepoStore._artifact_record(path, content.encode("utf-8"))
        for path, content in sorted(files.items())
    ]
    manifest: dict[str, Any] = {
        "poc_id": poc_id,
        "spec_version": spec_version,
        "code_version": code_version,
        "storage": {"provider": "github", "repository": repository, "branch": GitHubRepoStore.branch_for_poc(poc_id)},
        "inputs": {"schema_design": dict(schema_input)},
        "artifacts": artifacts,
        "correlation": dict(correlation),
        "producer": "poc-data-seed",
    }
    if repair is not None:
        manifest["repair"] = dict(repair)
    files[f"{prefix}/seed.manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    return files


def build_project_seed_bundle_files(
    *,
    repository: str,
    branch: str,
    poc_id: str,
    code_version: str,
    seed_js: str,
    package_json: str,
    seed_readme: str,
    data_model_input: Mapping[str, Any],
    correlation: Mapping[str, str],
    defaults_applied: list[str],
    repair_notes: str | None = None,
    repair: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build one top-level immutable seed/vNNN bundle and manifest."""
    GitHubRepoStore._safe_branch(branch)
    GitHubRepoStore._version(code_version, "code_version")
    required_correlation = {"poc_id", "run_id", "task_id", "trace_id", "producer"}
    if set(correlation) != required_correlation or correlation.get("poc_id") != poc_id:
        raise ValueError("correlation must contain matching poc_id, run_id, task_id, trace_id, and producer")
    inputs = {"data_model": dict(data_model_input)}
    item = inputs["data_model"]
    if item.get("key") != "spec_architect/data_model.json" or not _SHA256_PATTERN.fullmatch(str(item.get("sha256", ""))):
        raise ValueError("seed manifest data_model must use its canonical path and SHA-256 digest")
    if not _COMMIT_SHA_PATTERN.fullmatch(str(item.get("commit_sha", ""))):
        raise ValueError("seed manifest data_model requires an exact commit SHA")
    prefix = f"seed/{code_version}"
    files = {
        f"{prefix}/seed.js": seed_js,
        f"{prefix}/package.json": package_json,
        f"{prefix}/SEED_README.md": seed_readme,
    }
    if repair_notes is not None:
        files[f"{prefix}/REPAIR_NOTES.md"] = repair_notes
    if bool(repair_notes is not None) != bool(repair is not None):
        raise ValueError("repair bundles require both repair notes and repair metadata")
    manifest: dict[str, Any] = {
        "poc_id": poc_id,
        "code_version": code_version,
        "storage": {"provider": "github", "repository": repository, "branch": branch},
        "inputs": inputs,
        "artifacts": [
            GitHubRepoStore._artifact_record(path, content.encode("utf-8"))
            for path, content in sorted(files.items())
        ],
        "defaults_applied": list(defaults_applied),
        "correlation": dict(correlation),
        "producer": "poc-data-seed",
    }
    if repair is not None:
        manifest["repair"] = dict(repair)
    files[f"{prefix}/seed.manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    return files
