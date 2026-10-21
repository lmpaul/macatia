"""`ralph-kit close`: the parent closure of a parent issue whose children are implemented."""

from __future__ import annotations

import argparse
import pathlib
import shlex
import sys
from collections.abc import Callable
from typing import Any

from .commands import RalphError
from .config import RalphConfig, RalphConfigError, load_config
from .github import GitHub
from .loop_core import DEFAULT_CONFIG_PATH, author_login, labels, positive_integer


COMPLETED = "completed"
# Marks the parent's closure comment, so a rerun after the parent failed to close does not repeat it.
CLOSURE_MARKER = "<!-- ralph-parent-closure -->"


class CloseRefused(RalphError):
    """A check failed, or no one confirmed: nothing was written."""


class CloseCancelled(Exception):
    """Ctrl-C at the confirmation: nothing was written."""


def parse_arguments(argv: list[str], prog: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Close a parent issue's open children, then the parent, as completed.",
    )
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        default=DEFAULT_CONFIG_PATH,
        help="repository-specific Ralph TOML configuration",
    )
    parser.add_argument("--yes", action="store_true", help="close without asking for confirmation")
    parser.add_argument("parent_issue", type=positive_integer)
    return parser.parse_args(argv)  # Exits with status 2 on usage errors.


def is_open(issue: dict[str, Any]) -> bool:
    return str(issue.get("state", "")).upper() == "OPEN"


def issue_reference(issue: dict[str, Any]) -> str:
    return f"#{issue['number']} {issue.get('title', '')}".rstrip()


def failed_checks(
    config: RalphConfig, parent: dict[str, Any], children: list[dict[str, Any]]
) -> list[str]:
    """Every reason the parent closure cannot proceed; closed children are ignored."""
    human_review = config.labels["human_review"]
    implemented = config.labels["implemented"]
    trusted = ", ".join(f"@{login}" for login in sorted(config.trusted_authors))
    failures = []
    if human_review not in labels(parent):
        failures.append(f"#{parent['number']} is not labelled {human_review}")
    if author_login(parent) not in config.trusted_authors:
        failures.append(f"#{parent['number']} is authored by @{author_login(parent)}, not one of: {trusted}")
    if not children:
        failures.append(f"#{parent['number']} has no sub-issues")
    for child in children:
        if not is_open(child):
            continue
        if author_login(child) not in config.trusted_authors:
            failures.append(f"#{child['number']} is authored by @{author_login(child)}, not one of: {trusted}")
        lifecycle = labels(child) & config.lifecycle_labels
        if lifecycle != {implemented}:
            shown = ", ".join(sorted(lifecycle)) or "none"
            failures.append(
                f"#{child['number']} is not implemented (lifecycle labels: {shown}; "
                f"expected only {implemented})"
            )
    return failures


def closure_comment(closed: list[dict[str, Any]]) -> str:
    if not closed:
        return f"{CLOSURE_MARKER}\nRalph closed this parent issue: every child was already closed."
    lines = "\n".join(f"- {issue_reference(child)}" for child in closed)
    return (
        f"{CLOSURE_MARKER}\nRalph closed this parent issue after closing its children "
        f"as completed:\n\n{lines}"
    )


def has_closure_comment(config: RalphConfig, parent: dict[str, Any]) -> bool:
    """Whether the parent's latest trusted comment is a closure comment."""
    trusted = [
        comment for comment in parent.get("comments", [])
        if author_login(comment) in config.trusted_authors
    ]
    return bool(trusted) and CLOSURE_MARKER in str(trusted[-1].get("body", ""))


def close_parent(
    github: Any,
    config: RalphConfig,
    parent_number: int,
    *,
    yes: bool,
    confirm: Callable[[str], bool] | None,
    rerun: str,
) -> None:
    """Check everything, confirm, then close the open children, comment on and close the parent."""
    try:
        parent = github.issue(parent_number)
    except RalphError as error:
        raise RalphError(f"Cannot read #{parent_number}, nothing closed: {error}") from error
    if int(parent.get("number", 0)) != parent_number:
        raise RalphError(f"GitHub returned the wrong issue for #{parent_number}")
    if not is_open(parent):
        print(f"#{parent_number} is already closed.")
        return
    children = github.sub_issues(parent_number)
    if failures := failed_checks(config, parent, children):
        raise CloseRefused(
            f"Cannot close #{parent_number}:\n"
            + "\n".join(f"  - {failure}" for failure in failures)
            + "\nNothing closed."
        )
    open_children = [child for child in children if is_open(child)]
    print(f"Parent closure of {issue_reference(parent)}, as completed:")
    for child in open_children:
        print(f"  close child {issue_reference(child)}")
    print(f"  then close parent {issue_reference(parent)}")
    if not yes:
        if confirm is None:
            raise CloseRefused(
                "Confirmation needs a terminal; rerun with --yes to close without it. Nothing closed."
            )
        try:
            confirmed = confirm(f"Close these {len(open_children) + 1} issues?")
        except KeyboardInterrupt:
            raise CloseCancelled from None
        if not confirmed:
            raise CloseRefused("Declined. Nothing closed.")
    closed: list[dict[str, Any]] = []
    try:
        for child in open_children:
            github.close_issue(int(child["number"]), COMPLETED)
            closed.append(child)
            print(f"Closed #{child['number']}.")
        if closed or not has_closure_comment(config, parent):
            github.comment_on_issue(parent_number, closure_comment(closed))
        github.close_issue(parent_number, COMPLETED)
    except RalphError as error:
        done = ", ".join(f"#{child['number']}" for child in closed) or "none"
        raise RalphError(f"{error}\nClosed so far: {done}. Rerun `{rerun}` to finish.") from error
    print(f"Closed #{parent_number}.")
    print(f"✅ Closed #{parent_number} and its children.")


def run(
    argv: list[str],
    *,
    github: Callable[[pathlib.Path, str], Any] = GitHub,
    confirm: Callable[[str], bool] | None = None,
    repository_root: pathlib.Path | None = None,
    prog: str = "ralph-kit close",
) -> int:
    """Run `ralph-kit close`; `confirm` asks a Yes/No question and is None without a terminal.

    Exits 0 once closed or already closed, 1 on a refusal, a declined
    confirmation or a GitHub error, 2 on a usage error, 130 on Ctrl-C.
    """
    arguments = parse_arguments(argv, prog)
    root = (repository_root or pathlib.Path.cwd()).resolve()
    config_path = arguments.config if arguments.config.is_absolute() else root / arguments.config
    rerun = shlex.join([
        *prog.split(),
        *(["--config", str(arguments.config)] if arguments.config != DEFAULT_CONFIG_PATH else []),
        str(arguments.parent_issue),
    ])
    try:
        config = load_config(config_path.resolve(), root, require_review=False)
        close_parent(
            github(root, config.repository),
            config,
            arguments.parent_issue,
            yes=arguments.yes,
            confirm=confirm,
            rerun=rerun,
        )
    except CloseCancelled:
        print("\nCancelled. Nothing closed.", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        print(f"\nInterrupted. Rerun `{rerun}` to finish.", file=sys.stderr)
        return 130
    except (RalphError, RalphConfigError) as error:
        print(f"❌ {error}", file=sys.stderr)
        return 1
    return 0
