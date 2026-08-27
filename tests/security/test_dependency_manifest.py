from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _requirement_names(path: Path) -> set[str]:
    names: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-r "):
            continue
        match = re.match(r"([A-Za-z0-9_.-]+)(?:\[.*\])?==", line)
        if match:
            names.add(match.group(1).lower())
    return names


class DependencyManifestTests(unittest.TestCase):
    def test_runtime_manifest_declares_backend_imports(self) -> None:
        requirements = _requirement_names(ROOT / "requirements.txt")

        expected = {
            "bcrypt",
            "email-validator",
            "fastapi",
            "google-genai",
            "gunicorn",
            "loguru",
            "numpy",
            "psycopg2-binary",
            "pydantic",
            "python-dotenv",
            "python-jose",
            "redis",
            "requests",
            "sentence-transformers",
            "uvicorn",
        }

        self.assertTrue(
            expected.issubset(requirements),
            f"Missing runtime requirements: {sorted(expected - requirements)}",
        )

    def test_dev_manifest_separates_audit_and_test_tooling(self) -> None:
        runtime_requirements = _requirement_names(ROOT / "requirements.txt")
        dev_text = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        dev_requirements = _requirement_names(ROOT / "requirements-dev.txt")

        self.assertIn("-r requirements.txt", dev_text)
        self.assertNotIn("pip-audit", runtime_requirements)
        self.assertIn("pip-audit", dev_requirements)
        self.assertIn("httpx", dev_requirements)

    def test_dockerfile_installs_manifest_and_serves_backend_app(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY requirements.txt .", dockerfile)
        self.assertIn("ENV PYTHONPATH=/app:/app/backend", dockerfile)
        self.assertIn("pip install --prefix=/install --no-cache-dir -r requirements.txt", dockerfile)
        self.assertIn('"backend.api:app"', dockerfile)

    def test_dependency_scan_workflow_uses_pip_audit(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "dependency-scan.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("python-version: \"3.11\"", workflow)
        self.assertIn("pip-audit==2.7.3", workflow)
        self.assertIn("python -m pip_audit -r requirements.txt", workflow)
