"""The contributor contract must be enforced, not merely described.

AGENTS.md tells an agent what done means, tools/pr_gate.sh runs it locally and
.github/workflows/gate.yml runs it on every pull request. If the three drift,
an agent can be told a change is finished while CI rejects it, or CI can stop
checking a gate the local script still claims to check. These tests keep the
three copies of the gate list in agreement.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

GATES = [
    "make test",
    "python3 -m logos test",
    "bash tools/accept.sh",
    "python3 -m logos guide --check",
    "python3 -m logos fmt $(git diff --name-only main...HEAD -- '*.logos')",
]

WORKFLOW = ROOT / ".github" / "workflows" / "gate.yml"
PR_TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
ISSUE_TEMPLATE = ROOT / ".github" / "ISSUE_TEMPLATE" / "bug.yml"
AGENTS = ROOT / "AGENTS.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
PR_GATE = ROOT / "tools" / "pr_gate.sh"
README = ROOT / "README.md"


def stripped_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]


class ContributorFilesTest(unittest.TestCase):
    """Every document a contributor is told to read or fill in exists."""

    def test_required_files_exist(self):
        for path in (
            AGENTS,
            CONTRIBUTING,
            PR_TEMPLATE,
            ISSUE_TEMPLATE,
            WORKFLOW,
            PR_GATE,
        ):
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertTrue(path.is_file(), f"{path} is missing")

    def test_agents_points_at_the_manual_first(self):
        text = AGENTS.read_text(encoding="utf-8")
        self.assertIn("docs/LOGOS.md", text)
        self.assertIn("Read `docs/LOGOS.md` first", text)

    def test_readme_has_a_contribute_section(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("## Contribute", text)
        for pointer in ("AGENTS.md", "CONTRIBUTING.md", "tools/pr_gate.sh"):
            with self.subTest(pointer=pointer):
                self.assertIn(pointer, text)
        self.assertIn("AI agents", text)

    def test_pr_gate_is_executable_and_parses(self):
        self.assertTrue(os.access(PR_GATE, os.X_OK), "tools/pr_gate.sh is not executable")
        first = PR_GATE.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first, "#!/usr/bin/env bash")
        result = subprocess.run(
            ["bash", "-n", str(PR_GATE)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class GateListAgreementTest(unittest.TestCase):
    """AGENTS.md, the local gate script and CI name the same five commands."""

    def test_agents_names_the_five_gates_in_order(self):
        lines = stripped_lines(AGENTS)
        positions = []
        for command in GATES:
            with self.subTest(command=command):
                self.assertIn(command, lines)
                positions.append(lines.index(command))
        self.assertEqual(positions, sorted(positions), "AGENTS.md gates are out of order")

    def test_pr_gate_runs_the_five_gates(self):
        lines = stripped_lines(PR_GATE)
        for command in GATES:
            with self.subTest(command=command):
                self.assertIn(command, lines)

    def test_workflow_runs_the_five_gates(self):
        lines = stripped_lines(WORKFLOW)
        for command in GATES:
            with self.subTest(command=command):
                self.assertIn(command, lines)

    def test_workflow_is_the_required_job(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("name: gate", text)
        self.assertIn("runs-on: ubuntu-22.04", text)
        self.assertIn("pull_request", text)
        self.assertIn("branches: [main]", text)
        for package in ("clang", "lld", "gcc", "jq", "python3"):
            with self.subTest(package=package):
                self.assertIn(package, text)
        self.assertNotIn("continue-on-error", text, "a failing gate must stop the job")


class TemplatesTest(unittest.TestCase):
    """The templates ask for the evidence the gates require."""

    def test_pull_request_template_mirrors_the_gates(self):
        text = PR_TEMPLATE.read_text(encoding="utf-8")
        for command in GATES:
            with self.subTest(command=command):
                self.assertIn(command, text)
        for required in (
            "regress",
            "failed before the fix",
            "summary.txt",
            "never",
            "invariants",
            "unrelated reformatting",
            "feat/",
            "fix/",
            "docs/",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_bug_form_asks_for_a_reproduction(self):
        text = ISSUE_TEMPLATE.read_text(encoding="utf-8")
        for required in (
            "Minimal Logos program",
            "Command",
            "Expected output",
            "Actual output or diagnostic code",
            '"-O0"',
            '"-O2"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()
