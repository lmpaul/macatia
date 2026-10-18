"""Process tests for the standard run; GitHub and Codex remain offline."""

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from harness import RalphProcessTestCase

# Imported from the kit, outside the repository each run is started from.
from ralph_kit import loop


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
