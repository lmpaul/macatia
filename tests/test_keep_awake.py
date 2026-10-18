"""`ralph-kit run --caffeinate`: the sleep lock helper and its CLI flag."""

import contextlib
import io
import pathlib
import shutil
import subprocess
import sys
import unittest
from unittest import mock


KIT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT / "src"))

from ralph_kit import cli, keep_awake, loop, loop_review


class HelperCommandTests(unittest.TestCase):
    def test_macos_uses_caffeinate_bound_to_the_run(self):
        self.assertEqual(keep_awake.helper_command("darwin", 42), ["caffeinate", "-i", "-w", "42"])

    def test_linux_uses_systemd_inhibit_bound_to_the_run(self):
        command = keep_awake.helper_command("linux", 42)
        self.assertEqual(command[:3], ["systemd-inhibit", "--what=idle:sleep", "--who=ralph-kit"])
        self.assertEqual(command[-4:], ["tail", "--pid=42", "-f", "/dev/null"])

    def test_other_platforms_have_no_helper(self):
        self.assertIsNone(keep_awake.helper_command("freebsd14", 42))


class KeepAwakeTests(unittest.TestCase):
    def test_unsupported_platform_warns_and_runs_the_block(self):
        stderr = io.StringIO()
        ran = False
        with contextlib.redirect_stderr(stderr), keep_awake.keep_awake("freebsd14"):
            ran = True
        self.assertTrue(ran)
        self.assertIn("--caffeinate ignored: not supported on freebsd14", stderr.getvalue())

    def test_missing_helper_warns_and_runs_the_block(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(keep_awake.shutil, "which", return_value=None),
            contextlib.redirect_stderr(stderr),
            keep_awake.keep_awake("linux"),
        ):
            pass
        self.assertIn("--caffeinate ignored: systemd-inhibit not found", stderr.getvalue())

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("caffeinate"), "needs macOS caffeinate")
    def test_macos_holds_the_assertion_only_during_the_block(self):
        def caffeinate_running():
            listing = subprocess.run(["pgrep", "-f", f"caffeinate -i -w {keep_awake.os.getpid()}"], capture_output=True)
            return listing.returncode == 0

        with keep_awake.keep_awake("darwin"):
            self.assertTrue(caffeinate_running())
        self.assertFalse(caffeinate_running())


class CaffeinateFlagTests(unittest.TestCase):
    def run_cli(self, *arguments):
        entered = []

        @contextlib.contextmanager
        def fake_keep_awake():
            entered.append(True)
            yield

        with (
            mock.patch.object(keep_awake, "keep_awake", fake_keep_awake),
            mock.patch.object(loop, "run", return_value=0) as standard,
            mock.patch.object(loop_review, "run", return_value=0) as reviewed,
            mock.patch.object(sys, "dont_write_bytecode", sys.dont_write_bytecode),
        ):
            self.assertEqual(cli.main(["run", *arguments]), 0)
        return bool(entered), standard, reviewed

    def test_flag_wraps_a_standard_run_in_any_position(self):
        for arguments in (["--caffeinate", "5"], ["5", "--caffeinate"]):
            with self.subTest(arguments=arguments):
                entered, standard, reviewed = self.run_cli(*arguments)
                self.assertTrue(entered)
                standard.assert_called_once_with(["5"], prog="ralph-kit run")
                reviewed.assert_not_called()

    def test_flag_combines_with_review(self):
        entered, standard, reviewed = self.run_cli("--review", "--caffeinate", "5")
        self.assertTrue(entered)
        reviewed.assert_called_once_with(["5"], prog="ralph-kit run --review")
        standard.assert_not_called()

    def test_without_flag_the_machine_may_sleep(self):
        entered, standard, _ = self.run_cli("5")
        self.assertFalse(entered)
        standard.assert_called_once_with(["5"], prog="ralph-kit run")


if __name__ == "__main__":
    unittest.main()
