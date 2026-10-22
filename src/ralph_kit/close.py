"""`ralph-kit close`: the closure of a run target once its work is merged: a parent issue's
implemented children, then the parent, or a standalone issue alone."""

from __future__ import annotations

import argparse
import pathlib
import shlex
import sys
from collections.abc import Callable
from typing import Any, NamedTuple

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
from .progress import spinner


COMPLETED = "completed"
NO_EVIDENCE = "implemented without evidence: merge not verified"
# Marks the parent's closure comment, so a rerun after the parent failed to close does not repeat it.
CLOSURE_MARKER = "<!-- ralph-parent-closure -->"


class Failure(NamedTuple):
    """A failed check: a short summary, and the detail that explains it, if any."""

    summary: str
    detail: str = ""

    def terminal(self) -> str:
        return f"⚠️  {self.summary}" + (f"\n    ↳ {self.detail}" if self.detail else "")

    def markdown(self) -> str:
        return f"- {self.summary}" + (f": {self.detail}" if self.detail else "")


class CloseRefused(RalphError):
    """A check failed, or no one confirmed: nothing was written."""


class CloseCancelled(Exception):
    """Ctrl-C at the confirmation: nothing was written."""


def parse_arguments(argv: list[str], prog: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Close a parent issue's open children, then the parent, or a standalone issue alone, "
            "as completed."
        ),
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
        help="close despite failed checks, after typing the issue number at a terminal",
    )
    parser.add_argument("issue", type=positive_integer)
    return parser.parse_args(argv)  # Exits with status 2 on usage errors.


def is_open(issue: dict[str, Any]) -> bool:
    return str(issue.get("state", "")).upper() == "OPEN"


def issue_reference(issue: dict[str, Any]) -> str:
    return f"#{issue['number']} {issue.get('title', '')}".rstrip()


def failed_checks(
    github: Any,
    config: RalphConfig,
    target: dict[str, Any],
    children: list[dict[str, Any]],
    *,
    standalone: bool,
) -> tuple[list[Failure], list[dict[str, Any]], dict[int, int]]:
    """Every reason the closure cannot proceed, the open children whose merge is not
    verified: without trusted implementation evidence, and the number of the merged
    pull request that proves each child merged by one. A child without evidence that
    is implemented is not a failure; only --force closes the others.

    `children` are the open children's details, comments included; a standalone
    `target` is its own only child, checked as one, and needs no human review.
    """
    human_review = config.labels["human_review"]
    implemented = config.labels["implemented"]
    trusted = ", ".join(f"@{login}" for login in sorted(config.trusted_authors))
    failures: list[Failure] = []
    without_evidence = []
    merged_by: dict[int, int] = {}
    if not standalone and human_review not in labels(target):
        failures.append(Failure(f"#{target['number']} is not labelled {human_review}"))
    for issue in children if standalone else (target, *children):
        if author_login(issue) not in config.trusted_authors:
            failures.append(Failure(
                f"#{issue['number']} is authored by @{author_login(issue)}",
                f"trusted authors: {trusted}",
            ))
    for child in children:
        lifecycle = labels(child) & config.lifecycle_labels
        if lifecycle != {implemented}:
            shown = ", ".join(sorted(lifecycle)) or "none"
            failures.append(Failure(
                f"#{child['number']} is not implemented",
                f"lifecycle labels: {shown} (expected only {implemented})",
            ))
        heads = latest_implementation_heads(child, config.trusted_authors, STANDARD_RUN)
        if heads is None:
            without_evidence.append(child)
        # Ancestry first: a child merged by ancestry costs no pull request lookup.
        elif not github.commit_is_on_branch(config.base_branch, heads[-1]):
            pulls = github.merged_pull_requests(config.base_branch, heads[-1])
            if pulls:
                merged_by[int(child["number"])] = pulls[0].number
            else:
                failures.append(Failure(
                    f"#{child['number']} might not be merged",
                    f"verified head {heads[-1][:7]} is neither on {config.base_branch} "
                    "nor in a pull request merged into it",
                ))
    return failures, without_evidence, merged_by


def child_numbers(children: list[dict[str, Any]]) -> str:
    return ", ".join(f"#{child['number']}" for child in children)


def forced_section(failures: list[Failure]) -> str:
    if not failures:
        return "The closure was forced with --force; every check passed."
    return "The closure was forced with --force despite these failed checks:\n\n" + "\n".join(
        failure.markdown() for failure in failures
    )


def merged_child_reference(child: dict[str, Any], merged_by: dict[int, int]) -> str:
    pull = merged_by.get(int(child["number"]))
    return issue_reference(child) + ("" if pull is None else f", merged by PR #{pull}")


def closure_comment(
    closed: list[dict[str, Any]],
    without_evidence: list[dict[str, Any]],
    merged_by: dict[int, int],
    *,
    standalone: dict[str, Any] | None,
    has_children: bool,
    forced_failures: list[Failure] | None,
) -> str:
    """The closure comment on the run target, `standalone` when it is a standalone issue;
    `forced_failures` is None unless the closure was forced."""
    forced = "" if forced_failures is None else "\n\n" + forced_section(forced_failures)
    if standalone is not None:
        if standalone in without_evidence:
            how = ", as confirmed by a human without implementation evidence"
        else:
            pull = merged_by.get(int(standalone["number"]))
            how = "" if pull is None else f", merged by PR #{pull}"
        return f"{CLOSURE_MARKER}\nRalph closed this standalone issue as completed{how}.{forced}"
    if not has_children:
        return f"{CLOSURE_MARKER}\nRalph closed this issue, which has no sub-issues.{forced}"
    if not closed:
        return f"{CLOSURE_MARKER}\nRalph closed this parent issue: every child was already closed.{forced}"
    sections = [
        heading + "\n".join(f"- {merged_child_reference(child, merged_by)}" for child in group)
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


def confirm_typed(number: int, type_answer: Callable[[str], str] | None) -> None:
    """Ask a human at a terminal to type the issue number; refuse on anything else."""
    if type_answer is None:
        raise CloseRefused("--force needs a human at a terminal to type the issue number. Nothing closed.")
    try:
        answer = type_answer(f"Type {number} to force the closure: ")
    except KeyboardInterrupt:
        raise CloseCancelled from None
    except EOFError:
        answer = ""
    if answer.strip() != str(number):
        raise CloseRefused(f"Did not type {number}. Nothing closed.")


def close_target(
    github: Any,
    config: RalphConfig,
    number: int,
    *,
    yes: bool,
    force: bool,
    confirm: Callable[[str], bool] | None,
    type_answer: Callable[[str], str] | None,
    rerun: str,
) -> None:
    """Check everything, confirm, then close the open children, comment on and close the issue.

    An issue without a parent or sub-issues is a standalone issue, checked as a child and
    closed alone. An issue with a parent fails a check naming it: the parent is the run target.
    Under `force`, failed checks are warnings and the confirmation is the typed issue number.
    """
    with spinner(f"Checking #{number} and its sub-issues before closing…"):
        try:
            target = github.issue(number)
        except RalphError as error:
            raise RalphError(f"Cannot read #{number}, nothing closed: {error}") from error
        if int(target.get("number", 0)) != number:
            raise RalphError(f"GitHub returned the wrong issue for #{number}")
        if is_open(target):
            try:
                parent = github.parent(number)
            except RalphError as error:
                raise RalphError(
                    f"Cannot read the parent of #{number}, nothing closed: {error}"
                ) from error
            children = github.sub_issues(number)
            standalone = target if parent is None and not children else None
            # Sub-issue listings carry no comments: each open child's evidence is in its details.
            open_children = [
                github.issue(int(child["number"])) for child in children if is_open(child)
            ]
            failures, without_evidence, merged_by = failed_checks(
                github,
                config,
                target,
                open_children if standalone is None else [target],
                standalone=standalone is not None,
            )
            if parent is not None:
                failures.insert(0, Failure(
                    f"#{number} is a sub-issue of #{parent['number']}",
                    f"close #{parent['number']} instead",
                ))
    if not is_open(target):
        print(f"#{number} is already closed.")
        return
    if failures and not force:
        raise CloseRefused(
            f"Cannot close #{number}:\n\n"
            + "\n".join(failure.terminal() for failure in failures)
            + "\n\nNothing closed."
        )
    for failure in failures:
        print(failure.terminal())
    if failures:
        print()
    closing = "Force-closing" if force else "Closing"
    if standalone is not None:
        print(f"📋 {closing} standalone issue {merged_child_reference(target, merged_by)}, as completed.")
        if without_evidence:
            print(f"   ❓ {NO_EVIDENCE}")
    else:
        print(f"📋 {closing} {issue_reference(target)}, as completed:")
        for child in open_children:
            if child not in without_evidence:
                print(f"   → {merged_child_reference(child, merged_by)}")
        if without_evidence:
            print(f"   ❓ {NO_EVIDENCE}:")
            for child in without_evidence:
                print(f"      → {issue_reference(child)}")
        print(f"   → then #{number}")
    if force:
        confirm_typed(number, type_answer)
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
            confirmed = confirm(
                f"Close these {len(open_children) + 1} issues?"
                if open_children
                else f"Close #{number}?"
            )
        except KeyboardInterrupt:
            raise CloseCancelled from None
        if not confirmed:
            raise CloseRefused("Declined. Nothing closed.")
    closed: list[dict[str, Any]] = []
    try:
        for child in open_children:
            github.close_issue(int(child["number"]), COMPLETED)
            closed.append(child)
            print(f"   ✓ #{child['number']}")
        if closed or not has_closure_comment(config, target):
            github.comment_on_issue(
                number,
                closure_comment(
                    closed,
                    without_evidence,
                    merged_by,
                    standalone=standalone,
                    has_children=bool(children),
                    forced_failures=failures if force else None,
                ),
            )
        github.close_issue(number, COMPLETED)
    except RalphError as error:
        done = child_numbers(closed) or "none"
        raise RalphError(f"{error}\nClosed so far: {done}. Rerun `{rerun}` to finish.") from error
    print(f"   ✓ #{number}")
    count = len(closed)
    print(
        f"✅ Closed #{number}"
        + (f" and {count} {'child' if count == 1 else 'children'}" if count else "")
        + "."
    )



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
        str(arguments.issue),
    ])
    try:
        config = load_config(config_path.resolve(), root, require_review=False)
        close_target(
            github(root, config.repository),
            config,
            arguments.issue,
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
