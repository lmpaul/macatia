import unittest

from harness import RalphProcessTestCase


class ReviewLoopTests(RalphProcessTestCase):
    RUN_OPTIONS = ("--review",)

    def assert_only_safe_terminal_summary_was_published(self):
        github_calls = self._json_lines(self.github_log)
        self.assertFalse(
            any(
                call[:2] in (["pr", "create"], ["issue", "edit"])
                for call in github_calls
            )
        )
        issue_comments = [
            call for call in github_calls if call[:2] == ["issue", "comment"]
        ]
        self.assertEqual(len(issue_comments), 1)
        self.assertEqual(issue_comments[0][:3], ["issue", "comment", "191"])
        self.assertIn("<!-- ralph-terminal-summary -->", issue_comments[0][-1])
        self.assertIn("final outcome: stopped safely.", issue_comments[0][-1])

    def test_claude_cli_failure_reports_exit_code_and_stderr(self):
        self._configure_engines(implementation="claude")
        result = self._run_loop("191", environment=self.environment | {"RALPH_TEST_CLAUDE_CLI_ERROR": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Claude Code exited with code 1: Error: --json-schema is not a valid JSON Schema",
            result.stderr,
        )
        self.assertFalse(self.codex_log.exists())
        self.assert_only_safe_terminal_summary_was_published()


if __name__ == "__main__":
    unittest.main()
