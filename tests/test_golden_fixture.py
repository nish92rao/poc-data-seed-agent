"""Integrity tests for the checked-in accepted golden seed bundle."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


GOLDEN_ROOT = Path(__file__).parent / "golden" / "subscription_billing" / "v013"


class GoldenFixtureTests(unittest.TestCase):
    def test_manifest_identity_and_all_bound_hashes_match(self) -> None:
        manifest = json.loads((GOLDEN_ROOT / "seed.manifest.json").read_text(encoding="utf-8"))
        provenance = json.loads((GOLDEN_ROOT / "provenance.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["poc_id"], provenance["poc_id"])
        self.assertEqual(manifest["spec_version"], provenance["spec_version"])
        self.assertEqual(manifest["code_version"], provenance["code_version"])
        self.assertEqual(manifest["storage"]["provider"], "github")
        self.assertEqual(manifest["storage"]["repository"], provenance["repository"])
        self.assertEqual(manifest["storage"]["branch"], provenance["branch"])

        expected_artifacts = {"seed.js", "package.json", "SEED_README.md"}
        self.assertEqual(
            {entry["key"].rsplit("/", 1)[-1] for entry in manifest["artifacts"]},
            expected_artifacts,
        )
        for entry in manifest["artifacts"]:
            filename = entry["key"].rsplit("/", 1)[-1]
            content = (GOLDEN_ROOT / filename).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), entry["sha256"])
            self.assertEqual(len(content), entry["bytes"])

        for name, filename in (
            ("schema_design", "schema_design.json"),
            ("query_patterns", "query_patterns.json"),
        ):
            content = (GOLDEN_ROOT / filename).read_bytes()
            self.assertEqual(
                hashlib.sha256(content).hexdigest(),
                manifest["inputs"][name]["sha256"],
            )

    def test_provenance_pins_accepted_source_and_report_commits(self) -> None:
        provenance = json.loads((GOLDEN_ROOT / "provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(
            provenance["source_commit_sha"],
            "c9c8736a302644e583a4a514347df37155fbd00b",
        )
        self.assertEqual(
            provenance["report_commit_sha"],
            "37aa9d873f1e3fc64ee97b52add4aca19c7b4173",
        )
        self.assertEqual(provenance["validation_status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
