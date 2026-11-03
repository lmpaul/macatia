"""The persistent terminal: the session a run restarts in with `--tmux`, and the tmux command
line that starts it."""

import pathlib
import sys
import unittest
from unittest import mock


KIT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT / "src"))

from macatia import persistent_terminal


class PersistentTerminalNameTests(unittest.TestCase):
    def test_names_the_repository_and_the_run_target(self):
        self.assertEqual(
            persistent_terminal.persistent_terminal_name("lmpaul/macatia", 129),
            "macatia-macatia-129",
        )

    def test_dots_and_colons_become_underscores(self):
        self.assertEqual(
            persistent_terminal.persistent_terminal_name("someone/my.app:v2", 7),
            "macatia-my_app_v2-7",
        )


class TmuxCommandTests(unittest.TestCase):
    def test_starts_the_named_session_in_the_directory_running_the_command_then_the_shell(self):
        self.assertEqual(
            persistent_terminal.tmux_command(
                "macatia-buyr-191",
                pathlib.Path("/work/buyr"),
                "macatia run --review 191",
                "/bin/zsh",
            ),
            [
                "tmux", "new-session", "-s", "macatia-buyr-191", "-c", "/work/buyr",
                "macatia run --review 191; exec /bin/zsh",
            ],
        )

    def test_quotes_a_shell_path_with_spaces(self):
        command = persistent_terminal.tmux_command(
            "macatia-buyr-191", pathlib.Path("/work/buyr"), "macatia run 191", "/opt/my shell"
        )
        self.assertEqual(command[-1], "macatia run 191; exec '/opt/my shell'")


class UnavailableTests(unittest.TestCase):
    def unavailable(self, tmux, environment):
        with (
            mock.patch.object(persistent_terminal.shutil, "which", return_value=tmux) as which,
            mock.patch.dict(persistent_terminal.os.environ, environment, clear=True),
        ):
            reason = persistent_terminal.PersistentTerminal().unavailable()
        which.assert_called_with("tmux")
        return reason

    def test_available_with_tmux_outside_tmux(self):
        self.assertIsNone(self.unavailable("/usr/bin/tmux", {}))

    def test_tmux_not_found(self):
        self.assertIs(
            self.unavailable(None, {"TMUX": "/tmp/tmux-1000/default,1,0"}),
            persistent_terminal.Unavailability.TMUX_NOT_FOUND,
        )

    def test_already_inside_tmux(self):
        self.assertIs(
            self.unavailable("/usr/bin/tmux", {"TMUX": "/tmp/tmux-1000/default,1,0"}),
            persistent_terminal.Unavailability.INSIDE_TMUX,
        )

    def test_each_reason_says_what_to_do(self):
        self.assertEqual(
            [str(reason) for reason in persistent_terminal.Unavailability],
            [
                "tmux not found: install tmux, or run without --tmux",
                "already inside tmux: run without --tmux",
            ],
        )


class ExistsTests(unittest.TestCase):
    def exists(self, returncode):
        completed = mock.Mock(returncode=returncode)
        with mock.patch.object(
            persistent_terminal.subprocess, "run", return_value=completed
        ) as run:
            exists = persistent_terminal.PersistentTerminal().exists("macatia-buyr-191")
        run.assert_called_once_with(
            ["tmux", "has-session", "-t", "=macatia-buyr-191"],
            stdout=persistent_terminal.subprocess.DEVNULL,
            stderr=persistent_terminal.subprocess.DEVNULL,
            check=False,
        )
        return exists

    def test_a_session_of_exactly_that_name_exists(self):
        self.assertTrue(self.exists(0))

    def test_no_such_session_or_no_tmux_server(self):
        self.assertFalse(self.exists(1))


class RestartHereTests(unittest.TestCase):
    def test_replaces_this_process_with_the_command_through_the_shell_in_the_directory(self):
        with (
            mock.patch.dict(persistent_terminal.os.environ, {"SHELL": "/bin/zsh"}, clear=True),
            mock.patch.object(persistent_terminal.os, "chdir") as chdir,
            mock.patch.object(persistent_terminal.os, "execvp") as execvp,
        ):
            persistent_terminal.restart_here(
                "macatia run --worktree 191", pathlib.Path("/work/buyr.worktrees/issue-191")
            )
        chdir.assert_called_once_with(pathlib.Path("/work/buyr.worktrees/issue-191"))
        execvp.assert_called_once_with(
            "/bin/zsh", ["/bin/zsh", "-c", "macatia run --worktree 191"]
        )


if __name__ == "__main__":
    unittest.main()
