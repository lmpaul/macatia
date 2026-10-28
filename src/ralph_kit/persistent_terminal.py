"""The persistent terminal: a tmux session a run restarts itself in with `--tmux`, so it
continues after the person's own terminal disconnects."""

from __future__ import annotations

import enum
import os
import pathlib
import shlex
import sys
from typing import NamedTuple, NoReturn


QUESTION = "Where should this run continue?"


class Continuation(enum.Enum):
    """The answers to the continuation choice."""

    RESTART = "restart"
    CONTINUE_HERE = "continue here"
    STOP = "stop"


class ContinuationChoice(NamedTuple):
    answer: Continuation
    label: str
    description: str


def continuation_choices(name: str) -> list[ContinuationChoice]:
    """The choices of the continuation choice offering persistent terminal `name`, the
    default first."""
    return [
        ContinuationChoice(
            Continuation.RESTART,
            f"Restart in {name}",
            "the run restarts in a persistent terminal and attaches to it",
        ),
        ContinuationChoice(
            Continuation.CONTINUE_HERE, "Continue here", "the run continues in this terminal"
        ),
        ContinuationChoice(Continuation.STOP, "Stop", "the run stops with nothing changed"),
    ]


def persistent_terminal_name(repository: str, run_target: int) -> str:
    """The name of the persistent terminal of `run_target` in the GitHub `repository`
    (owner/name); tmux reads `.` and `:` in a session name as separators."""
    name = f"ralph-kit-{repository.split('/', 1)[-1]}-{run_target}"
    return name.replace(".", "_").replace(":", "_")


def tmux_command(name: str, directory: pathlib.Path, command: str, shell: str) -> list[str]:
    """The tmux command line that starts session `name` in `directory` and attaches to it,
    running the shell `command`, then `shell`, so the session outlives the run."""
    return [
        "tmux", "new-session", "-s", name, "-c", str(directory),
        f"{command}; exec {shlex.quote(shell)}",
    ]


class PersistentTerminal:
    """tmux, as the run reaches it."""

    def restart(self, name: str, command: str, directory: pathlib.Path) -> NoReturn:
        """Replace this process with a new session `name` in `directory` running the shell
        `command`, attached to this terminal."""
        arguments = tmux_command(name, directory, command, os.environ.get("SHELL") or "sh")
        sys.stdout.flush()
        sys.stderr.flush()
        os.execvp(arguments[0], arguments)
