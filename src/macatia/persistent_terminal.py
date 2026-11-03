"""The persistent terminal: a tmux session a run restarts itself in with `--tmux`, so it
continues after the person's own terminal disconnects."""

from __future__ import annotations

import enum
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
from typing import NamedTuple, NoReturn


QUESTION = "Where should this run continue?"


class Continuation(enum.Enum):
    """The answers to the continuation choice."""

    RESTART = "restart"
    CONTINUE_HERE = "continue here"
    STOP = "stop"


class Unavailability(enum.Enum):
    """Why a persistent terminal cannot be used, as the error that says what to do."""

    TMUX_NOT_FOUND = "tmux not found: install tmux, or run without --tmux"
    INSIDE_TMUX = "already inside tmux: run without --tmux"

    def __str__(self) -> str:
        return self.value


def already_exists(name: str) -> str:
    """The error that stops a `--tmux` run whose persistent terminal `name` already exists,
    saying how to get back to it."""
    return f"{name} already exists: attach to it with tmux attach -t {name}, or end it first"


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
    name = f"macatia-{repository.split('/', 1)[-1]}-{run_target}"
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

    def unavailable(self) -> Unavailability | None:
        """Why a persistent terminal cannot be used here, or None when it can."""
        if shutil.which("tmux") is None:
            return Unavailability.TMUX_NOT_FOUND
        if os.environ.get("TMUX"):
            return Unavailability.INSIDE_TMUX
        return None

    def exists(self, name: str) -> bool:
        """Whether a tmux session of exactly `name` exists; `=` stops tmux matching a prefix."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", f"={name}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def restart(self, name: str, command: str, directory: pathlib.Path) -> NoReturn:
        """Replace this process with a new session `name` in `directory` running the shell
        `command`, attached to this terminal."""
        arguments = tmux_command(name, directory, command, os.environ.get("SHELL") or "sh")
        sys.stdout.flush()
        sys.stderr.flush()
        os.execvp(arguments[0], arguments)


def restart_here(command: str, directory: pathlib.Path) -> NoReturn:
    """Replace this process with the shell `command` run in `directory`, in this terminal."""
    shell = os.environ.get("SHELL") or "sh"
    sys.stdout.flush()
    sys.stderr.flush()
    os.chdir(directory)
    os.execvp(shell, [shell, "-c", command])
