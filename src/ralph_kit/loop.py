"""`ralph-kit run`: the run entry point; startup selects the standard or reviewed run."""

from __future__ import annotations

from collections.abc import Callable

from . import loop_core
from .persistent_terminal import Continuation


def run(
    argv: list[str] | None = None,
    prog: str | None = None,
    confirm: Callable[..., bool] | None = None,
    choose_run_target: Callable[[list[loop_core.StartableRunTarget], int | None], int] | None = None,
    choose_continuation: Callable[[str], Continuation] | None = None,
) -> int:
    return loop_core.run(
        loop_core.real_run_adapters(confirm, choose_run_target, choose_continuation), argv, prog
    )
