"""The sleep lock helper, and the CLI passing `--caffeinate` and `--no-caffeinate` to the run."""

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

from macatia import cli, keep_awake, loop


class HelperCommandTests(unittest.TestCase):
    def test_macos_uses_caffeinate_bound_to_the_run(self):
        self.assertEqual(keep_awake.helper_command("darwin", 42), ["caffeinate", "-i", "-w", "42"])

    def test_linux_uses_systemd_inhibit_bound_to_the_run(self):
        command = keep_awake.helper_command("linux", 42)
        self.assertEqual(command[:3], ["systemd-inhibit", "--what=idle:sleep", "--who=macatia"])
        self.assertEqual(command[-4:], ["tail", "--pid=42", "-f", "/dev/null"])

    def test_other_platforms_have_no_helper(self):
        self.assertIsNone(keep_awake.helper_command("freebsd14", 42))


class AvailabilityTests(unittest.TestCase):
    def test_installed_helper_is_available(self):
        with mock.patch.object(keep_awake.shutil, "which", return_value="/usr/bin/systemd-inhibit"):
            self.assertEqual(keep_awake.availability("linux"), ("systemd-inhibit", None))

    def test_missing_helper_is_named(self):
        with mock.patch.object(keep_awake.shutil, "which", return_value=None):
            self.assertEqual(keep_awake.availability("darwin"), ("caffeinate", "caffeinate not found"))

    def test_unsupported_platform_has_no_helper(self):
        self.assertEqual(keep_awake.availability("freebsd14"), ("", "not supported on freebsd14"))


class KeepAwakeTests(unittest.TestCase):
    def test_unsupported_platform_warns_and_runs_the_block(self):
        stderr = io.StringIO()
        ran = False
        with contextlib.redirect_stderr(stderr), keep_awake.keep_awake("freebsd14"):
            ran = True
        self.assertTrue(ran)
        self.assertIn("No sleep lock: not supported on freebsd14", stderr.getvalue())

    def test_missing_helper_warns_and_runs_the_block(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(keep_awake.shutil, "which", return_value=None),
            contextlib.redirect_stderr(stderr),
            keep_awake.keep_awake("linux"),
        ):
            pass
        self.assertIn("No sleep lock: systemd-inhibit not found", stderr.getvalue())

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("caffeinate"), "needs macOS caffeinate")
    def test_macos_holds_the_assertion_only_during_the_block(self):
        def caffeinate_running():
            listing = subprocess.run(["pgrep", "-f", f"caffeinate -i -w {keep_awake.os.getpid()}"], capture_output=True)
            return listing.returncode == 0

        with keep_awake.keep_awake("darwin"):
            self.assertTrue(caffeinate_running())
        self.assertFalse(caffeinate_running())


class CaffeinateFlagTests(unittest.TestCase):
    """`macatia run` leaves `--caffeinate` and `--no-caffeinate` to the run, which decides on
    the sleep lock at startup."""

    def test_cli_passes_the_flags_through_to_the_run_unchanged(self):
        for arguments in (["--review", "5", "--caffeinate"], ["--no-caffeinate", "5"]):
            with self.subTest(arguments=arguments):
                self.assert_passed_through(arguments)

    def test_usage_lists_both_flags(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["--help"]), 0)
        self.assertIn("[--caffeinate | --no-caffeinate]", stdout.getvalue())
        self.assertIn("--no-caffeinate lets the machine sleep", stdout.getvalue())

    def assert_passed_through(self, arguments):
        with (
            mock.patch.object(loop, "run", return_value=0) as run,
            mock.patch.object(sys, "dont_write_bytecode", sys.dont_write_bytecode),
        ):
            self.assertEqual(cli.main(["run", *arguments]), 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], arguments)
        self.assertEqual(run.call_args.kwargs["prog"], "macatia run")


if __name__ == "__main__":
    unittest.main()
