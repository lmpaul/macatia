"""Process tests for the standard run; GitHub and Codex remain offline."""

import contextlib
import io
import json
import os
import re
import sys
import unittest
from unittest import mock

from harness import RalphProcessTestCase, run

# Imported from the kit, outside the repository each run is started from.
from ralph_kit import loop


class StartLoopTests(RalphProcessTestCase):
    def run_loop(self, budget=None, **environment):
        settings = self.environment | environment
        args = ("191",) if budget is None else ("191", str(budget))
        return self._run_loop(*args, environment=settings)

    def test_verification_flags_require_toml_booleans(self):
        from ralph_kit.config import RalphConfigError, load_config

        config_path = self.repository / "ralph.toml"
        original = config_path.read_text()
        for key in ("local_checks", "ci_checks"):
            for value in ('"false"', '"true"', '0', '1'):
                with self.subTest(key=key, value=value):
                    config_path.write_text(original.replace(f"{key} = true", f"{key} = {value}"))
                    with self.assertRaisesRegex(RalphConfigError, rf"{key} must be a boolean"):
                        load_config(config_path, self.repository)
        config_path.write_text(original)

    def test_legacy_verification_keys_are_rejected(self):
        from ralph_kit.config import RalphConfigError, load_config

        config_path = self.repository / "ralph.toml"
        config_path.write_text(config_path.read_text().replace(
            "local_checks = true\nci_checks = true",
            'mode = "ci"\nlocal_gates = "enabled"',
        ))
        with self.assertRaisesRegex(RalphConfigError, "unknown keys: local_gates, mode"):
            load_config(config_path, self.repository)

    def calls(self):
        return self._json_lines(self.codex_log)

    def github_calls(self):
        return self._json_lines(self.github_log)

    def state(self):
        return json.loads(self.github_state.read_text())

    def child(self):
        return next(child for child in self.state()["children"] if child["number"] == 193)

    def assert_completed(self, result, count):
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), count)
        for call in calls:
            self.assertIn("gpt-5.6-terra", call["arguments"])
            self.assertIn('model_reasoning_effort="medium"', call["arguments"])
        self.assertEqual({label["name"] for label in self.child()["labels"]}, {"implemented"})
        self.assertFalse(self.state()["pull_request"]["isDraft"])
        body = self.child()["comments"][-1]["body"]
        self.assertIn("ralph-experimental-implementation-evidence", body)
        self.assertIn("No separate Ralph review session was run", body)
        self.assertNotIn("reviewed_head", body)

    def test_claude_implementation_and_correction_use_the_shared_adapter(self):
        self._configure_engines(implementation="claude", correction="claude")
        result = self.run_loop(RALPH_TEST_FAIL_GATE_COUNT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.codex_log.exists())
        calls = self._json_lines(self.claude_log)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["prompt"].startswith("/implement issue #193\n"))
        self.assertIn("Repair the failing local gate", calls[1]["prompt"])
        self.assertEqual({label["name"] for label in self.child()["labels"]}, {"implemented"})

    def test_local_and_ci_repairs_chain_previous_context_without_sol(self):
        self.assert_completed(self.run_loop(
            RALPH_TEST_FAIL_GATE_COUNT="1", RALPH_TEST_CI_OUTCOMES="failure,success",
        ), 3)
        self.assertIn("Previous session summary: Repaired the failing local gate.", self.calls()[2]["prompt"])
        self.assertEqual(len(self.quality_log.read_text().splitlines()), 3)

    def test_unfinished_requirements_are_fixed_before_ci(self):
        self.assert_completed(self.run_loop(RALPH_TEST_AGENT_RESULTS=json.dumps([
            {"outcome": "needs_correction", "commit": True, "findings": ["Implement the missing acceptance criterion."]},
            {"outcome": "committed"},
        ])), 2)
        self.assertIn("Implement the missing acceptance criterion.", self.calls()[1]["prompt"])

    def test_local_gate_ci_and_issue_corrections_share_the_correction_limit(self):
        self.assert_completed(self.run_loop(
            RALPH_TEST_FAIL_GATE_COUNT="1",
            RALPH_TEST_CI_OUTCOMES="failure,success",
            RALPH_TEST_AGENT_RESULTS=json.dumps([
                {"outcome": "committed"},
                {"outcome": "committed"},
                {"outcome": "needs_correction", "commit": True, "findings": ["Complete the journey."]},
                {"outcome": "committed"},
            ]),
        ), 4)
        prompts = [call["prompt"] for call in self.calls()]
        self.assertIn("Repair the failing local gate", prompts[1])
        self.assertIn("Correction attempt 1/3", prompts[1])
        self.assertIn("Repair the failing CI check", prompts[2])
        self.assertIn("Correction attempt 2/3", prompts[2])
        self.assertIn("Complete the remaining requirements", prompts[3])
        self.assertIn("Correction attempt 3/3", prompts[3])

    def test_ci_correction_requires_a_new_commit(self):
        result = self.run_loop(
            RALPH_TEST_CI_OUTCOMES="failure", RALPH_TEST_CI_CORRECTION_NO_CHANGES="1",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CI correction did not create a new commit", result.stderr)
        self.assertEqual({label["name"] for label in self.child()["labels"]}, {"human-review"})

    def test_review_profile_is_optional_for_a_standard_run(self):
        config = self.repository / "ralph.toml"
        config.write_text(re.sub(r"\[models.review\].*?(?=\[models.correction\])", "", config.read_text(), flags=re.S))
        run(["git", "commit", "-am", "Remove unused review profile"], cwd=self.repository)
        self.assert_completed(self.run_loop(), 1)

    def test_review_profile_remains_required_for_a_reviewed_run(self):
        config = self.repository / "ralph.toml"
        config.write_text(re.sub(r"\[models.review\].*?(?=\[models.correction\])", "", config.read_text(), flags=re.S))
        run(["git", "commit", "-am", "Remove required review profile"], cwd=self.repository)
        result = run(
            [sys.executable, "-m", "ralph_kit", "run", "--review", "191"],
            cwd=self.repository, env=self.environment, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("configuration.models is missing keys: review", result.stderr)
        self.assertFalse(self.codex_log.exists())

    def test_dirty_agent_output_cannot_be_published(self):
        result = self.run_loop(RALPH_TEST_LEAVE_DIRTY="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("worktree is dirty", result.stderr)
        self.assertIsNone(self.state()["pull_request"])


class RepeatedRunTests(RalphProcessTestCase):
    """Runs started one after the other in one Python process."""

    def run_in_process(self):
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.environment, clear=True),
            contextlib.chdir(self.repository),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            returncode = loop.run(["191"], prog="ralph-kit run")
        self.assertEqual(returncode, 0, stderr.getvalue())

    def test_second_run_does_not_share_the_first_runs_state(self):
        self.run_in_process()
        self.run_in_process()
        first, second = [
            comment["body"]
            for comment in json.loads(self.github_state.read_text())["parent"]["comments"]
            if "ralph-experimental-terminal-summary" in comment["body"]
        ]
        self.assertIn("implemented children this run: 1;", first)
        self.assertIn("agent_sessions: 1;", first)
        self.assertIn("implemented children this run: 0;", second)
        self.assertIn("agent_sessions: 0;", second)
        runs = list((self.repository / ".ralph" / "diagnostics").glob("*/run.json"))
        self.assertEqual(len(runs), 2)


if __name__ == "__main__":
    unittest.main()
