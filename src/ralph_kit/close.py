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
from .loop_core import (
    DEFAULT_CONFIG_PATH,
    STANDARD_RUN,
    author_login,
    labels,
    latest_implementation_heads,
    positive_integer,
)


COMPLETED = "completed"
NO_EVIDENCE = "implemented without evidence: merge not verified"
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
    confirmation = parser.add_mutually_exclusive_group()
    confirmation.add_argument("--yes", action="store_true", help="close without asking for confirmation")
    confirmation.add_argument(
        "--force",
        action="store_true",
        help="close despite failed checks, after typing the parent number at a terminal",
    )
    parser.add_argument("parent_issue", type=positive_integer)
    return parser.parse_args(argv)  # Exits with status 2 on usage errors.


def is_open(issue: dict[str, Any]) -> bool:
    return str(issue.get("state", "")).upper() == "OPEN"


def issue_reference(issue: dict[str, Any]) -> str:
    return f"#{issue['number']} {issue.get('title', '')}".rstrip()


def failed_checks(
    github: Any, config: RalphConfig, parent: dict[str, Any], children: list[dict[str, Any]]
) -> tuple[list[str], list[dict[str, Any]]]:
    """Every reason the parent closure cannot proceed, and the open children whose merge
    is not verified: without trusted implementation evidence. One that is implemented
    is not a failure; only --force closes the others.

    `children` are the open children's details, comments included.
    """
    human_review = config.labels["human_review"]
    implemented = config.labels["implemented"]
    trusted = ", ".join(f"@{login}" for login in sorted(config.trusted_authors))
    failures = []
    without_evidence = []
    if human_review not in labels(parent):
        failures.append(f"#{parent['number']} is not labelled {human_review}")
    if author_login(parent) not in config.trusted_authors:
        failures.append(f"#{parent['number']} is authored by @{author_login(parent)}, not one of: {trusted}")
    for child in children:
        if author_login(child) not in config.trusted_authors:
            failures.append(f"#{child['number']} is authored by @{author_login(child)}, not one of: {trusted}")
        lifecycle = labels(child) & config.lifecycle_labels
        if lifecycle != {implemented}:
            shown = ", ".join(sorted(lifecycle)) or "none"
            failures.append(
                f"#{child['number']} is not implemented (lifecycle labels: {shown}; "
                f"expected only {implemented})"
            )
        heads = latest_implementation_heads(child, config.trusted_authors, STANDARD_RUN)
        if heads is None:
            without_evidence.append(child)
        elif not github.commit_is_on_branch(config.base_branch, heads[-1]):
            failures.append(
                f"#{child['number']} is not merged: its verified head {heads[-1]} "
                f"is not on {config.base_branch}"
            )
    return failures, without_evidence


def child_numbers(children: list[dict[str, Any]]) -> str:
    return ", ".join(f"#{child['number']}" for child in children)


def forced_section(failures: list[str]) -> str:
    if not failures:
        return "The closure was forced with --force; every check passed."
    return "The closure was forced with --force despite these failed checks:\n\n" + "\n".join(
        f"- {failure}" for failure in failures
    )


def closure_comment(
    closed: list[dict[str, Any]],
    without_evidence: list[dict[str, Any]],
    *,
    has_children: bool,
    forced_failures: list[str] | None,
) -> str:
    """The parent's closure comment; `forced_failures` is None unless the closure was forced."""
    forced = "" if forced_failures is None else "\n\n" + forced_section(forced_failures)
    if not has_children:
        return f"{CLOSURE_MARKER}\nRalph closed this issue, which has no sub-issues.{forced}"
    if not closed:
        return f"{CLOSURE_MARKER}\nRalph closed this parent issue: every child was already closed.{forced}"
    sections = [
        heading + "\n".join(f"- {issue_reference(child)}" for child in group)
        for heading, group in (
            ("", [child for child in closed if child not in without_evidence]),
            (
                "Closed as confirmed by a human without implementation evidence:\n\n",
                [child for child in closed if child in without_evidence],
            ),
        )
        if group
    ]
    return (
        f"{CLOSURE_MARKER}\nRalph closed this parent issue after closing its children "
        f"as completed:\n\n" + "\n\n".join(sections) + forced
    )


def has_closure_comment(config: RalphConfig, parent: dict[str, Any]) -> bool:
    """Whether the parent's latest trusted comment is a closure comment."""
    trusted = [
        comment for comment in parent.get("comments", [])
        if author_login(comment) in config.trusted_authors
    ]
    return bool(trusted) and CLOSURE_MARKER in str(trusted[-1].get("body", ""))


def confirm_typed(parent_number: int, type_answer: Callable[[str], str] | None) -> None:
    """Ask a human at a terminal to type the parent number; refuse on anything else."""
    if type_answer is None:
        raise CloseRefused("--force needs a human at a terminal to type the parent number. Nothing closed.")
    try:
        answer = type_answer(f"Type {parent_number} to force the closure: ")
    except KeyboardInterrupt:
        raise CloseCancelled from None
    except EOFError:
        answer = ""
    if answer.strip() != str(parent_number):
        raise CloseRefused(f"Did not type {parent_number}. Nothing closed.")


def close_parent(
    github: Any,
    config: RalphConfig,
    parent_number: int,
    *,
    yes: bool,
    force: bool,
    confirm: Callable[[str], bool] | None,
    type_answer: Callable[[str], str] | None,
    rerun: str,
) -> None:
    """Check everything, confirm, then close the open children, comment on and close the parent.

    Under `force`, failed checks are warnings and the confirmation is the typed parent number.
    """
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
    # Sub-issue listings carry no comments: each open child's evidence is in its details.
    open_children = [
        github.issue(int(child["number"])) for child in children if is_open(child)
    ]
    failures, without_evidence = failed_checks(github, config, parent, open_children)
    if not children:
        failures.append(f"#{parent_number} has no sub-issues")
    if failures and not force:
        raise CloseRefused(
            f"Cannot close #{parent_number}:\n"
            + "\n".join(f"  - {failure}" for failure in failures)
            + "\nNothing closed."
        )
    for failure in failures:
        print(f"⚠️ {failure}")
    print(f"Parent closure of {issue_reference(parent)}, as completed{', forced' if force else ''}:")
    for child in open_children:
        if child not in without_evidence:
            print(f"  close child {issue_reference(child)}")
    if without_evidence:
        print(f"  close children {NO_EVIDENCE}:")
        for child in without_evidence:
            print(f"    {issue_reference(child)}")
    print(f"  then close parent {issue_reference(parent)}")
    if force:
        confirm_typed(parent_number, type_answer)
    elif without_evidence or not yes:
        if confirm is None:
            raise CloseRefused(
                (
                    f"{child_numbers(without_evidence)}: {NO_EVIDENCE}; only a human at a terminal "
                    "can confirm closing them, --yes does not. Nothing closed."
                )
                if without_evidence
                else "Confirmation needs a terminal; rerun with --yes to close without it. "
                "Nothing closed."
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
            github.comment_on_issue(
                parent_number,
                closure_comment(
                    closed,
                    without_evidence,
                    has_children=bool(children),
                    forced_failures=failures if force else None,
                ),
            )
        github.close_issue(parent_number, COMPLETED)
    except RalphError as error:
        done = child_numbers(closed) or "none"
        raise RalphError(f"{error}\nClosed so far: {done}. Rerun `{rerun}` to finish.") from error
    print(f"Closed #{parent_number}.")
    print(f"✅ Closed #{parent_number}{' and its children' if children else ''}.")



def run(
    argv: list[str],
    *,
    github: Callable[[pathlib.Path, str], Any] = GitHub,
    confirm: Callable[[str], bool] | None = None,
    type_answer: Callable[[str], str] | None = None,
    repository_root: pathlib.Path | None = None,
    prog: str = "ralph-kit close",
) -> int:
    """Run `ralph-kit close`; `confirm` asks a Yes/No question and `type_answer` reads a typed
    answer, both None without a terminal.

    Exits 0 once closed or already closed, 1 on a refusal, a declined
    confirmation or a GitHub error, 2 on a usage error, 130 on Ctrl-C.
    """
    arguments = parse_arguments(argv, prog)
    root = (repository_root or pathlib.Path.cwd()).resolve()
    config_path = arguments.config if arguments.config.is_absolute() else root / arguments.config
    rerun = shlex.join([
        *prog.split(),
        *(["--force"] if arguments.force else []),
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
            force=arguments.force,
            confirm=confirm,
            type_answer=type_answer,
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
