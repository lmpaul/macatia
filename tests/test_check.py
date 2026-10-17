"""Offline readiness checks; no real agent or GitHub invocation."""

import contextlib
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock


KIT_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY = KIT_ROOT / "src"
FIXTURES = KIT_ROOT / "tests" / "fixtures"
sys.path.insert(0, str(SOURCE_DIRECTORY))

from ralph_kit import check


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name).resolve()
        (self.root / "policy").mkdir()
        shutil.copy2(FIXTURES / "ralph.toml", self.root / "ralph.toml")
        shutil.copy2(FIXTURES / "quality.py", self.root / "policy/quality.py")
        self.checker = check
        self.config = self.root / "ralph.toml"
        text = self.config.read_text().replace("lmpaul/buyr", "example/project")
        text = text.replace('["lmpaul"]', '["reader"]')
        text = text.replace("gpt-5.6-terra", "chosen-model")
        text = text.replace(".github/scripts/quality.py", "policy/quality.py")
        text = text.replace(".github/quality-gates.json", "policy/quality-gates.json")
        self.config.write_text(text)
        self.policy = self.root / "policy/quality-gates.json"
        self.policy.write_text(json.dumps({
            "version": 1, "path_groups": {},
            "local_gates": [{"id": "tests", "paths": ["**"], "before_agent_review": True, "command": ["python3"]}],
            "ci": {"checks": {"project": {"name": "Project CI", "command": ["python3"]}}, "baseline": ["project"], "final": ["project"], "conditional": []},
        }))
        skill = self.root / ".agents/skills/implement"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("Implement")
        self.branch = "feature"
        self.dirty = ""
        self.repo = "example/project"
        self.login = "reader"
        self.permission = True
        self.labels = ["needs-triage", "needs-info", "ready-for-agent", "ready-for-human", "wontfix", "human-review", "implemented"]
        self.commands = []

    def command(self, report, *args):
        self.commands.append(args)
        responses = {
            ("git", "rev-parse", "--show-toplevel"): str(self.root),
            ("git", "check-ref-format", "--branch", "main"): "",
            ("git", "remote", "get-url", "origin"): "git@github.com:example/project.git",
            ("git", "symbolic-ref", "--quiet", "--short", "HEAD"): self.branch,
            ("git", "status", "--porcelain"): self.dirty,
            ("gh", "repo", "view", "--json", "nameWithOwner"): json.dumps({"nameWithOwner": self.repo}),
            ("gh", "api", "user"): json.dumps({"login": self.login}),
            ("gh", "api", "repos/example/project"): json.dumps({"permissions": {"push": self.permission}}),
            ("gh", "api", "--paginate", "--slurp", "repos/example/project/labels"): json.dumps([[{"name": name} for name in self.labels]]),
        }
        # Only the explicit read-only allowlist is accepted by the fixture.
        return responses[args]

    def run_check(self, missing=None):
        output = io.StringIO()
        with mock.patch.object(pathlib.Path, "cwd", return_value=self.root), mock.patch.object(self.checker.shutil, "which", side_effect=lambda name: None if name == missing else f"/tools/{name}"), mock.patch.object(self.checker.Readiness, "command", autospec=True, side_effect=self.command), contextlib.redirect_stdout(output):
            code = self.checker.run(self.root, self.config)
        return code, output.getvalue()

    def flags(self, local, ci):
        self.config.write_text(self.config.read_text().replace("local_checks = true", f"local_checks = {str(local).lower()}").replace("ci_checks = true", f"ci_checks = {str(ci).lower()}"))

    def test_all_four_combinations(self):
        original = self.config.read_text()
        for local, ci in ((True, True), (True, False), (False, True), (False, False)):
            with self.subTest(local=local, ci=ci):
                self.config.write_text(original)
                self.flags(local, ci)
                self.assertEqual(self.run_check()[0], 0)

    def test_disabled_policy_is_not_loaded(self):
        self.flags(False, False)
        self.policy.unlink()
        (self.root / "policy/quality.py").unlink()
        self.assertEqual(self.run_check()[0], 0)

    def test_disabled_local_placeholders_are_ignored(self):
        self.flags(False, True)
        self.policy.write_text(self.policy.read_text().replace('"python3"', '"REPLACE_COMMAND"'))
        # CI commands must still be configured when CI is active.
        manifest = json.loads(self.policy.read_text())
        manifest["ci"]["checks"]["project"]["command"] = ["python3"]
        self.policy.write_text(json.dumps(manifest))
        self.assertEqual(self.run_check()[0], 0)

    def test_disabled_ci_placeholders_are_ignored(self):
        self.flags(True, False)
        self.policy.write_text(self.policy.read_text().replace("Project CI", "REPLACE_CHECK"))
        self.assertEqual(self.run_check()[0], 0)

    def test_active_placeholders_block(self):
        self.policy.write_text(self.policy.read_text().replace('"python3"', '"REPLACE_COMMAND"'))
        self.assertEqual(self.run_check()[0], 1)

    def test_unconfigured_kit_stops_before_commands(self):
        self.config.write_text(self.config.read_text().replace("chosen-model", "CHOOSE_MODEL"))
        self.assertEqual(self.run_check()[0], 1)
        self.assertEqual(self.commands, [])

    def test_missing_agent_cli_blocks(self):
        self.assertEqual(self.run_check(missing="codex")[0], 1)

    def test_wrong_repository_blocks(self):
        self.repo = "other/repo"
        self.assertEqual(self.run_check()[0], 1)

    def test_base_branch_blocks(self):
        self.branch = "main"
        self.assertEqual(self.run_check()[0], 1)

    def test_dirty_worktree_blocks(self):
        self.dirty = " M file"
        self.assertEqual(self.run_check()[0], 1)

    def test_untrusted_account_blocks(self):
        self.login = "other"
        self.assertEqual(self.run_check()[0], 1)

    def test_missing_write_permission_blocks(self):
        self.permission = False
        self.assertEqual(self.run_check()[0], 1)

    def test_missing_label_blocks(self):
        self.labels.remove("ready-for-agent")
        self.assertEqual(self.run_check()[0], 1)

    def test_broken_claude_skill_link_blocks(self):
        self.config.write_text(self.config.read_text().replace('engine = "codex"', 'engine = "claude"'))
        self.assertEqual(self.run_check()[0], 1)
        skill = self.root / ".claude/skills/implement"
        skill.parent.mkdir(parents=True)
        skill.symlink_to("../../.agents/skills/implement")
        self.assertEqual(self.run_check()[0], 0)
        (self.root / ".agents/skills/implement/SKILL.md").unlink()
        self.assertEqual(self.run_check()[0], 1)

    def test_missing_config_points_to_init(self):
        self.config.unlink()
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("→ ralph-kit init", output)
        self.assertEqual(self.commands, [])

    def test_run_without_config_points_to_init(self):
        import subprocess

        self.config.unlink()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        for review in ([], ["--review"]):
            with self.subTest(review=review):
                result = subprocess.run(
                    [sys.executable, "-m", "ralph_kit", "run", *review, "1"],
                    cwd=self.root, capture_output=True, text=True,
                    env=os.environ | {"PYTHONPATH": str(SOURCE_DIRECTORY)},
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("ralph.toml not found: run `ralph-kit init` first", result.stderr)

    def test_missing_gh_skips_github_checks(self):
        code, output = self.run_check(missing="gh")
        self.assertEqual(code, 1)
        self.assertFalse([args for args in self.commands if args[0] == "gh"])
        self.assertIn("install GitHub CLI", output)
        self.assertEqual(output.count("not verified: gh missing"), 5)
        self.assertIn("1 blocker", output)

    def test_failures_show_the_fix(self):
        self.labels.remove("implemented")
        self.labels.remove("wontfix")
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("2 missing: implemented, wontfix", output)
        self.assertIn('for label in implemented wontfix; do gh label create "$label"; done', " ".join(output.split()))

    def test_command_failure_does_not_echo_cli_output(self):
        import subprocess

        result = subprocess.CompletedProcess(["gh"], 1, "sensitive token", "sensitive token")
        with (
            mock.patch.object(self.checker.subprocess, "run", return_value=result),
            self.assertRaises(RuntimeError) as error,
        ):
            self.checker.Readiness(self.root).command("gh", "api", "user")
        self.assertNotIn("sensitive", str(error.exception))


if __name__ == "__main__":
    unittest.main()
