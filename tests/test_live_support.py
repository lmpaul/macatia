"""The live session helper, offline: how an engine process failure is reported."""

import pathlib
import subprocess
import sys
import tempfile
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from live_support import LiveSessionFailed, run_live_session


class LiveSessionFailureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name)

    def run_failing(self, engine, returncode, stdout, stderr):
        def runner(root, arguments):
            return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)

        with self.assertRaises(LiveSessionFailed) as raised:
            run_live_session(engine, "model", "low", self.root, runner=runner)
        return str(raised.exception)

    def test_claude_process_failure_reports_the_exit_code_and_stderr(self):
        message = self.run_failing("claude", 1, "", "Error: unknown option '--effort'\n")

        self.assertIn("Claude Code exited with code 1", message)
        self.assertIn("Error: unknown option '--effort'", message)

    def test_codex_process_failure_reports_the_exit_code_and_stderr(self):
        message = self.run_failing("codex", 2, "", "error: model is not supported\n")

        self.assertIn("Codex exited with code 2", message)
        self.assertIn("error: model is not supported", message)

    def test_successful_exit_without_a_parseable_result_reports_stdout_and_stderr(self):
        message = self.run_failing("claude", 0, "not json", "warning: slow network")

        self.assertIn("Claude Code exited with code 0", message)
        self.assertIn("Claude Code did not return valid JSON output", message)
        self.assertIn("not json", message)
        self.assertIn("warning: slow network", message)


if __name__ == "__main__":
    unittest.main()
