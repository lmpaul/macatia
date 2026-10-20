"""`ralph-kit run`: the run entry point; startup selects the standard or reviewed run."""

from __future__ import annotations

from collections.abc import Callable

from . import loop_core


def run(
    argv: list[str] | None = None,
    prog: str | None = None,
    confirm: Callable[[str], bool] | None = None,
) -> int:
    return loop_core.run(loop_core.real_run_adapters(confirm), argv, prog)
