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
