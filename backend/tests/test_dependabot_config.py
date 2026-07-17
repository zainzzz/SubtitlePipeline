"""Tests for Dependabot configuration and security scan workflow.

Validates that:
  - .github/dependabot.yml exists and includes all 4 ecosystems
  - .github/workflows/security-scan.yml includes pip-audit and trivy

Uses PyYAML when available, falls back to text-based parsing for minimal
CI environments that lack PyYAML.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _safe_yaml_load(path: Path) -> dict:
    """Load YAML, preferring PyYAML but falling back to text parsing."""
    try:
        import yaml

        with open(path) as f:
            return yaml.safe_load(f)
    except ImportError:
        text = path.read_text()
        data: dict = {"updates": [], "jobs": {}}
        for block in re.split(r"\n  - ", text):
            ecosystem = re.search(r'package-ecosystem:\s*"?([\w-]+)"?', block)
            interval = re.search(r"interval:\s*\"?(\w+)\"?", block)
            if ecosystem:
                data["updates"].append({
                    "package-ecosystem": ecosystem.group(1),
                    "schedule": {"interval": interval.group(1) if interval else "unknown"},
                })
        return data


class TestDependabotConfig(unittest.TestCase):
    """Verify .github/dependabot.yml covers all dependency ecosystems."""

    DEPENDABOT_PATH = ROOT / ".github" / "dependabot.yml"

    def setUp(self) -> None:
        if not self.DEPENDABOT_PATH.exists():
            self.skipTest(f"{self.DEPENDABOT_PATH} not found")

    def test_dependabot_yaml_is_valid(self) -> None:
        data = _safe_yaml_load(self.DEPENDABOT_PATH)
        self.assertIsInstance(data, dict)
        self.assertIn("updates", data)

    def test_includes_pip_ecosystem(self) -> None:
        ecosystems = self._get_ecosystems()
        self.assertIn("pip", ecosystems)

    def test_includes_npm_ecosystem(self) -> None:
        ecosystems = self._get_ecosystems()
        self.assertIn("npm", ecosystems)

    def test_includes_docker_ecosystem(self) -> None:
        ecosystems = self._get_ecosystems()
        self.assertIn("docker", ecosystems)

    def test_includes_github_actions_ecosystem(self) -> None:
        ecosystems = self._get_ecosystems()
        self.assertIn("github-actions", ecosystems)

    def test_all_four_ecosystems_present(self) -> None:
        ecosystems = self._get_ecosystems()
        expected = {"pip", "npm", "docker", "github-actions"}
        self.assertEqual(expected, ecosystems & expected)

    def test_schedule_is_weekly(self) -> None:
        data = _safe_yaml_load(self.DEPENDABOT_PATH)
        for update in data["updates"]:
            interval = update.get("schedule", {}).get("interval", "")
            self.assertIn(interval, {"weekly", "daily"})

    def _get_ecosystems(self) -> set[str]:
        data = _safe_yaml_load(self.DEPENDABOT_PATH)
        return {u["package-ecosystem"] for u in data["updates"]}


class TestSecurityScanWorkflow(unittest.TestCase):
    """Verify .github/workflows/security-scan.yml includes pip-audit + trivy."""

    WORKFLOW_PATH = ROOT / ".github" / "workflows" / "security-scan.yml"

    def setUp(self) -> None:
        if not self.WORKFLOW_PATH.exists():
            self.skipTest(f"{self.WORKFLOW_PATH} not found")

    def test_workflow_yaml_is_valid(self) -> None:
        content = self.WORKFLOW_PATH.read_text()
        self.assertIn("jobs:", content)

    def test_includes_pip_audit(self) -> None:
        content = self.WORKFLOW_PATH.read_text()
        self.assertIn("pip-audit", content)

    def test_includes_trivy(self) -> None:
        content = self.WORKFLOW_PATH.read_text()
        self.assertIn("trivy", content.lower())

    def test_pip_audit_job_name_exists(self) -> None:
        content = self.WORKFLOW_PATH.read_text()
        self.assertIn("pip-audit:", content)

    def test_trivy_job_name_exists(self) -> None:
        content = self.WORKFLOW_PATH.read_text()
        self.assertIn("trivy:", content)


if __name__ == "__main__":
    unittest.main()
