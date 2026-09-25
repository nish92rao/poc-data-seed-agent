"""Regression checks for security-sensitive surfaces removed by the GitHub migration."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class RemovedStorageSurfaceTests(unittest.TestCase):
    def test_removed_s3_modules_remain_absent(self) -> None:
        for relative_path in (
            "src/agent_poc_data_seed/presigned_storage.py",
            "src/agent_poc_data_seed/s3_layout.py",
        ):
            self.assertFalse((ROOT / relative_path).exists(), relative_path)

    def test_runtime_dependencies_do_not_include_s3_clients(self) -> None:
        python_project = (ROOT / "pyproject.toml").read_text().lower()
        validator_package = json.loads((ROOT / "resources/validator/package.json").read_text())
        self.assertNotIn("boto3", python_project)
        self.assertNotIn("s3", validator_package.get("dependencies", {}))
        self.assertNotIn("@aws-sdk/client-s3", validator_package.get("dependencies", {}))

    def test_active_runtime_has_no_s3_or_presign_operations(self) -> None:
        sources = "\n".join(
            path.read_text()
            for path in (
                ROOT / "src/agent_poc_data_seed/tools.py",
                ROOT / "src/agent_poc_data_seed/main.py",
                ROOT / "resources/validator/handler.js",
            )
        ).lower()
        for forbidden in ("presign", "getobject", "putobject", "s3client", "boto3"):
            self.assertNotIn(forbidden, sources)

    def test_deployment_creates_only_health_and_direct_validation_routes(self) -> None:
        script = (ROOT / "resources/aws/start-seed-validator.sh").read_text()
        self.assertIn("for route_key in 'POST /v1/validations/direct' 'GET /health'", script)
        self.assertNotIn("s3:", script.lower())
        self.assertNotIn("create-bucket", script.lower())
        self.assertNotIn("create-presigned", script.lower())


if __name__ == "__main__":
    unittest.main()