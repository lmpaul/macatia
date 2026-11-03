"""Spinners for the slow steps, which otherwise look frozen; a terminal gets them, other output does not."""

from __future__ import annotations

import contextlib
import time
from typing import Any

from rich.console import Console
from rich.text import Text


def duration_text(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes} min {seconds:02d} s" if minutes else f"{seconds} s"


class Elapsed:
    """A spinner message followed by the time since it started, refreshed with the spinner."""

    def __init__(self, message: str):
        self.message = message
        self.started_at = time.monotonic()

    def __rich__(self) -> Text:
        return Text(f"{self.message} · {duration_text(time.monotonic() - self.started_at)}")


class Countdown:
    """A spinner message followed by the time left before a wait of `seconds` ends."""

    def __init__(self, message: str, seconds: float):
        self.message = message
        self.ends_at = time.monotonic() + seconds

    def __rich__(self) -> Text:
        return Text(f"{self.message} · {duration_text(max(0.0, self.ends_at - time.monotonic()))} left")


def spinner(message: str | Elapsed | Countdown, console: Console | None = None) -> contextlib.AbstractContextManager[Any]:
    """A transient spinner showing `message` while the block runs; none off a terminal.

    Output printed meanwhile goes above it. A block that asks a question, or lets a
    subprocess write to the terminal, must not run under it.
    """
    console = console or Console()
    isatty = getattr(console.file, "isatty", None)
    if not (isatty and isatty()):
        return contextlib.nullcontext()
    return console.status(message)
