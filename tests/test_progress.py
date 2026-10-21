"""Spinners: shown on a terminal with their message, absent elsewhere."""

import io
import pathlib
import sys
import unittest
from unittest import mock


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console  # noqa: E402

from ralph_kit import progress  # noqa: E402


class TerminalOutput(io.StringIO):
    def isatty(self):
        return True


class ProgressTests(unittest.TestCase):
    def test_a_terminal_sees_the_message_then_it_is_cleared(self):
        output = TerminalOutput()
        with progress.spinner("Reading #69…", Console(file=output)):
            pass
        self.assertIn("Reading #69…", output.getvalue())

    def test_other_output_gets_nothing(self):
        output = io.StringIO()
        with progress.spinner("Reading #69…", Console(file=output)):
            print("kept", file=output)
        self.assertEqual(output.getvalue(), "kept\n")

    def test_elapsed_follows_the_message(self):
        with mock.patch.object(progress.time, "monotonic", side_effect=[100.0, 225.0]):
            elapsed = progress.Elapsed("Running local gate: tests")
            self.assertEqual(str(elapsed.__rich__()), "Running local gate: tests · 2 min 05 s")

    def test_countdown_never_goes_below_zero(self):
        with mock.patch.object(progress.time, "monotonic", side_effect=[100.0, 110.0, 200.0]):
            countdown = progress.Countdown("Waiting for GitHub Actions to start", 30)
            self.assertEqual(str(countdown.__rich__()), "Waiting for GitHub Actions to start · 20 s left")
            self.assertEqual(str(countdown.__rich__()), "Waiting for GitHub Actions to start · 0 s left")


if __name__ == "__main__":
    unittest.main()
