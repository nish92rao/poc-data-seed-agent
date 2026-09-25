"""Canonical immutable paths for POC seed artifacts."""

from __future__ import annotations


def spec_key(poc_id: str, spec_version: str, filename: str) -> str:
    """Return one immutable POC specification artifact path."""
    return f"pocs/{poc_id}/spec/{spec_version}/{filename}"


def seed_key(poc_id: str, code_version: str, filename: str) -> str:
    """Return one immutable POC seed artifact path."""
    return f"pocs/{poc_id}/code/{code_version}/seed/{filename}"


def validation_report_key(poc_id: str, code_version: str, run_id: str) -> str:
    """Return a run-correlated validation report path under a code version."""
    return f"{seed_key(poc_id, code_version, 'validation')}/{run_id}/report.json"