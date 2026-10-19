"""Standard run (`ralph-kit run`): implementation and corrections, no review session."""

from __future__ import annotations

from collections.abc import Callable

from . import loop_core


MODE = loop_core.RunMode(
    review=False,
    description=(
        "Implement a parent issue with the configured agent, local gates, and exact-commit CI."
    ),
    diagnostics_label="run",
    # The "experimental" marker names predate the standard run becoming the default.
    # They are persisted in GitHub comments, so they must not change.
    implementation_evidence_prefix="<!-- ralph-experimental-implementation-evidence: ",
    parent_completion_marker="<!-- ralph-experimental-parent-completion -->",
    terminal_summary_marker="<!-- ralph-experimental-terminal-summary -->",
    evidence_head_key="verified_head",
    follow_up_role="correction",
    follow_up_commits="correction commits",
    gate_correction_scope="Preserve the issue acceptance criteria while fixing this gate.",
    unverified_warning=(
        "ℹ️ Ralph is running without automated verification: local gates and "
        "remote CI are explicitly disabled; human review is required before merge."
    ),
)


def run(
    argv: list[str] | None = None,
    prog: str | None = None,
    confirm: Callable[[str], bool] | None = None,
) -> int:
    return loop_core.run(MODE, loop_core.real_run_adapters(confirm), argv, prog)
