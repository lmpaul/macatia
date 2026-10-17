"""Experimental implementation-only loop with persisted correction evidence."""

from __future__ import annotations

from . import loop_core


MODE = loop_core.LoopMode(
    review=False,
    description=(
        "Implement a parent issue with the configured agent, local gates, and exact-commit CI."
    ),
    diagnostics_label="start-ralph.py",
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


def run(argv: list[str] | None = None, prog: str | None = None) -> int:
    return loop_core.run(MODE, argv, prog)
