"""`ralph-kit reset`: a reset of a run target, so a run can start on it as though no run had
happened: its children agent working, implemented, or in human review, then the run target
itself, ready for an agent again, each with a reset marker."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .close import Failure, is_open, issue_reference
from .commands import RalphError
from .config import RalphConfig, RalphConfigError, load_config
from .github import GitHub
from .loop_core import (
    DEFAULT_CONFIG_PATH,
    RESET_MARKER_PREFIX,
    STANDARD_RUN,
    author_login,
    labels,
    latest_implementation_heads,
    positive_integer,
)
from .progress import spinner


# The lifecycle states a reset makes ready for an agent again, as label roles; a child
# carrying several left the first one listed.
RESET_STATES = ("human_review", "agent_working", "implemented")


class ResetRefused(RalphError):
    """A check failed, or no one confirmed: nothing was written."""


class ResetCancelled(Exception):
    """Ctrl-C at the confirmation: nothing was written."""


@dataclass(frozen=True)
class ChildReset:
    """A child the reset makes ready for an agent again."""

    issue: dict[str, Any]
    # The label role of the lifecycle state the child leaves.
    previous_state: str
    # Whether an interrupted reset already posted its reset marker.
    marked: bool


@dataclass(frozen=True)
class Plan:
    """What resetting an open run target writes."""

    target: dict[str, Any]
    standalone: bool
    # The children to reset now, in order; a standalone issue is its own only child.
    children: list[ChildReset]
    # The children an interrupted reset already made ready for an agent, by number, with
    # the label role of the state each left: read from their reset markers.
    already_reset: dict[int, str]
    # The run target's labels to remove; a parent issue then gains ready-for-agent.
    remove: list[str]


def parse_arguments(argv: list[str], prog: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Reset a run target so a run can start on it as though no run had happened: make it "
            "and its children agent working, implemented or in human review ready for an agent again."
        ),
    )
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        default=DEFAULT_CONFIG_PATH,
        help="repository-specific Ralph TOML configuration",
    )
    parser.add_argument("--yes", action="store_true", help="reset without asking for confirmation")
    parser.add_argument("issue", type=positive_integer, metavar="ISSUE")
    return parser.parse_args(argv)  # Exits with status 2 on usage errors.


def reset_marker_comment(body: str) -> dict[str, Any] | None:
    """The fields a reset marker records, or None when `body` carries none or a malformed one."""
    match = re.search(re.escape(RESET_MARKER_PREFIX) + r"(\{.*?\}) -->", body)
    if match is None:
        return None
    try:
        marker = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return marker if isinstance(marker, dict) else None


def trusted_reset_markers(config: RalphConfig, issue: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """The issue's trusted reset markers, oldest first, each with its comment's index."""
    markers = []
    for index, comment in enumerate(issue.get("comments", [])):
        if author_login(comment) not in config.trusted_authors:
            continue
        marker = reset_marker_comment(str(comment.get("body", "")))
        if marker is not None:
            markers.append((index, marker))
    return markers


def latest_recap_time(config: RalphConfig, target: dict[str, Any]) -> str | None:
    """When the run target's previous recap, its latest trusted reset marker, was posted."""
    markers = trusted_reset_markers(config, target)
    if not markers:
        return None
    return str(target["comments"][markers[-1][0]].get("createdAt", ""))


def pending_reset_marker(
    config: RalphConfig, child: dict[str, Any], since: str | None
) -> tuple[str | None, bool]:
    """The label role of the state the child left, as recorded by its latest trusted reset
    marker posted since the run target's previous recap, if any, and whether that marker
    is still the child's latest trusted comment: no run has written since."""
    markers = trusted_reset_markers(config, child)
    if not markers:
        return None, False
    index, marker = markers[-1]
    comments = child["comments"]
    if since is not None and not str(comments[index].get("createdAt", "")) > since:
        return None, False
    state = marker.get("previous_state")
    if state not in RESET_STATES:
        return None, False
    latest = not any(
        author_login(comment) in config.trusted_authors for comment in comments[index + 1:]
    )
    return state, latest


def previous_state(config: RalphConfig, issue: dict[str, Any]) -> str | None:
    """The label role of the lifecycle state a reset makes the issue leave, or None."""
    current = labels(issue)
    return next((role for role in RESET_STATES if config.labels[role] in current), None)


def is_merged(github: Any, config: RalphConfig, issue: dict[str, Any]) -> str | None:
    """The verified head of the issue's latest trusted implementation evidence when it is
    merged, as for a closure: on the base branch, or in a pull request merged into it."""
    heads = latest_implementation_heads(issue, config.trusted_authors, STANDARD_RUN)
    if heads is None:
        return None
    head = heads[-1]
    # Ancestry first: a child merged by ancestry costs no pull request lookup.
    if github.commit_is_on_branch(config.base_branch, head) or github.merged_pull_requests(
        config.base_branch, head
    ):
        return head
    return None


def inspect(github: Any, config: RalphConfig, target: dict[str, Any]) -> tuple[Plan, list[Failure]]:
    """The plan for resetting `target`, and every reason it cannot be reset."""
    number = int(target["number"])
    failures: list[Failure] = []
    try:
        parent = github.parent(number)
    except RalphError as error:
        raise RalphError(f"Cannot read the parent of #{number}, nothing reset: {error}") from error
    if parent is not None:
        failures.append(Failure(
            f"#{number} is a sub-issue of #{parent['number']}",
            f"reset #{parent['number']} instead",
        ))
    if author_login(target) not in config.trusted_authors:
        trusted = ", ".join(f"@{login}" for login in sorted(config.trusted_authors))
        failures.append(Failure(
            f"#{number} is authored by @{author_login(target)}", f"trusted authors: {trusted}"
        ))
    if not is_open(target):
        failures.append(Failure(f"#{number} is closed"))
    plan = Plan(target, False, [], {}, [])
    if failures:
        return plan, failures
    sub_issues = github.sub_issues(number)
    standalone = not sub_issues
    # Sub-issue listings carry no comments: each child's evidence is in its details.
    children = [target] if standalone else [github.issue(int(child["number"])) for child in sub_issues]
    since = None if standalone else latest_recap_time(config, target)
    resets: list[ChildReset] = []
    already_reset: dict[int, str] = {}
    for child in children:
        head = is_merged(github, config, child)
        if head is not None:
            failures.append(Failure(
                f"#{child['number']} is merged",
                f"verified head {head[:7]} is on {config.base_branch} or in a pull request "
                "merged into it; a reset never undoes merged work",
            ))
        if not is_open(child):
            continue
        marked_state, marked_last = pending_reset_marker(config, child, since)
        state = previous_state(config, child)
        if state is not None:
            resets.append(ChildReset(child, state, marked_last and marked_state == state))
        elif marked_state is not None and not standalone:
            already_reset[int(child["number"])] = marked_state
    # A standalone issue's labels are reset as its own only child's.
    remove = [] if standalone else sorted(
        labels(target) & ({config.labels["work_started"]} | config.lifecycle_labels)
        - {config.labels["ready_for_agent"]}
    )
    if not (resets or already_reset or remove):
        failures.append(Failure(
            f"#{number} is not resettable",
            f"#{number} is not agent working, implemented or in human review" if standalone
            else f"no child is agent working, implemented or in human review, and #{number} is "
            "marked neither work started nor human review",
        ))
    return Plan(target, standalone, resets, already_reset, remove), failures


def child_marker(config: RalphConfig, run_target: int, state: str) -> str:
    marker = json.dumps({"previous_state": state}, separators=(",", ":"))
    return (
        f"{RESET_MARKER_PREFIX}{marker} -->\n"
        f"A reset of #{run_target} made this child ready for an agent again; it was "
        f"`{config.labels[state]}`. Ralph reads none of its records above this comment."
    )


def standalone_recap(config: RalphConfig, state: str) -> str:
    marker = json.dumps({"previous_state": state}, separators=(",", ":"))
    return (
        f"{RESET_MARKER_PREFIX}{marker} -->\n"
        f"Ralph reset this standalone issue so a run can start on it as though no run had "
        f"happened: it was `{config.labels[state]}` and is ready for an agent again. "
        "Ralph reads none of its records above this comment."
    )


def parent_recap(config: RalphConfig, reset: dict[int, str]) -> str:
    children = [{"number": number, "previous_state": state} for number, state in sorted(reset.items())]
    marker = json.dumps({"children": children}, separators=(",", ":"))
    listing = (
        "It made these children ready for an agent again:\n\n" + "\n".join(
            f"- #{number}, was `{config.labels[state]}`" for number, state in sorted(reset.items())
        )
        if reset else "No child needed a reset."
    )
    return (
        f"{RESET_MARKER_PREFIX}{marker} -->\n"
        "Ralph reset this parent issue so a run can start on it as though no run had happened. "
        f"{listing}\n\nRalph reads none of its records above this comment."
    )


def print_plan(config: RalphConfig, plan: Plan) -> None:
    ready = config.labels["ready_for_agent"]
    number = int(plan.target["number"])
    working = config.labels["agent_working"]
    if plan.standalone:
        reset = plan.children[0]
        print(f"📋 Resetting standalone issue {issue_reference(plan.target)}:")
        print(f"   → {config.labels[reset.previous_state]} → {ready}, then a reset marker")
    else:
        print(f"📋 Resetting {issue_reference(plan.target)}:")
        for reset in plan.children:
            print(
                f"   → {issue_reference(reset.issue)}: "
                f"{', '.join(sorted(labels(reset.issue) & config.lifecycle_labels))} → {ready}, "
                "with a reset marker"
            )
        for child, state in sorted(plan.already_reset.items()):
            print(f"   ✓ #{child}: already reset (was {config.labels[state]})")
        change = ", ".join([*(f"-{name}" for name in plan.remove), f"+{ready}"])
        print(f"   → then #{number}: {change}, then the recap reset marker")
    for reset in plan.children:
        if working in labels(reset.issue):
            print(
                f"⚠️  #{reset.issue['number']} is {working}: a run may still be working on it; "
                "stop that run before confirming"
            )


def reset_target(
    github: Any,
    config: RalphConfig,
    number: int,
    *,
    yes: bool,
    confirm: Callable[[str], bool] | None,
    rerun: str,
) -> None:
    """Check everything, confirm, then reset the children and, last, the run target."""
    with spinner(f"Checking #{number} and any sub-issues before resetting…"):
        try:
            target = github.issue(number)
        except RalphError as error:
            raise RalphError(f"Cannot read #{number}, nothing reset: {error}") from error
        if int(target.get("number", 0)) != number:
            raise RalphError(f"GitHub returned the wrong issue for #{number}")
        plan, failures = inspect(github, config, target)
    if failures:
        raise ResetRefused(
            f"Cannot reset #{number}:\n\n"
            + "\n".join(failure.terminal() for failure in failures)
            + "\n\nNothing reset."
        )
    print_plan(config, plan)
    if not yes:
        if confirm is None:
            raise ResetRefused(
                "Confirmation needs a terminal; rerun with --yes to reset without it. Nothing reset."
            )
        try:
            confirmed = confirm(f"Reset #{number}?")
        except KeyboardInterrupt:
            raise ResetCancelled from None
        if not confirmed:
            raise ResetRefused("Declined. Nothing reset.")
    ready = config.labels["ready_for_agent"]
    try:
        reset = dict(plan.already_reset)
        for child in plan.children:
            child_number = int(child.issue["number"])
            # The marker comes first: a rerun after it reads the state the child left there.
            if not child.marked:
                github.comment_on_issue(
                    child_number,
                    standalone_recap(config, child.previous_state) if plan.standalone
                    else child_marker(config, number, child.previous_state),
                )
            stale = sorted(labels(child.issue) & config.lifecycle_labels - {ready})
            if plan.standalone:
                stale = sorted({*stale, *(labels(child.issue) & {config.labels["work_started"]})})
            github.edit_issue_labels(child_number, add=ready, remove=stale)
            reset[child_number] = child.previous_state
            if not plan.standalone:
                print(f"   ✓ #{child_number}")
        if not plan.standalone:
            if plan.remove or ready not in labels(plan.target):
                github.edit_issue_labels(number, add=ready, remove=plan.remove)
            # The recap comes last: its presence means the reset finished.
            github.comment_on_issue(number, parent_recap(config, reset))
    except RalphError as error:
        raise RalphError(f"{error}\nRerun `{rerun}` to finish the reset.") from error
    print(f"   ✓ #{number}")
    count = len(plan.children) if not plan.standalone else 0
    print(
        f"✅ Reset #{number}"
        + (f" and {count} {'child' if count == 1 else 'children'}" if count else "")
        + "."
    )


def run(
    argv: list[str],
    *,
    github: Callable[[pathlib.Path, str], Any] = GitHub,
    confirm: Callable[[str], bool] | None = None,
    repository_root: pathlib.Path | None = None,
    prog: str = "ralph-kit reset",
) -> int:
    """Run `ralph-kit reset`; `confirm` asks a Yes/No question, None without a terminal.

    Exits 0 once reset, 1 on a refusal, a declined confirmation or a GitHub error,
    2 on a usage error, 130 on Ctrl-C.
    """
    arguments = parse_arguments(argv, prog)
    root = (repository_root or pathlib.Path.cwd()).resolve()
    config_path = arguments.config if arguments.config.is_absolute() else root / arguments.config
    rerun = shlex.join([
        *prog.split(),
        *(["--config", str(arguments.config)] if arguments.config != DEFAULT_CONFIG_PATH else []),
        str(arguments.issue),
    ])
    try:
        config = load_config(config_path.resolve(), root, require_review=False)
        reset_target(
            github(root, config.repository),
            config,
            arguments.issue,
            yes=arguments.yes,
            confirm=confirm,
            rerun=rerun,
        )
    except ResetCancelled:
        print("\nCancelled. Nothing reset.", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        print(f"\nInterrupted. Rerun `{rerun}` to finish the reset.", file=sys.stderr)
        return 130
    except (RalphError, RalphConfigError) as error:
        print(f"❌ {error}", file=sys.stderr)
        return 1
    return 0
