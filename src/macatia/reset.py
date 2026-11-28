"""`macatia reset`: a reset of a run target, so a run can start on it as though no run had
happened: its open cumulative draft PR closed and its run branch deleted, then its children
agent working, implemented, or in human review, then the run target itself, ready for an
agent again, each with a reset marker, or, for a purge, without Macatia's comments. Without a
run target, it offers the resettable run targets to choose from."""

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

from .close import CLOSURE_MARKER, Failure, current_branch, is_open, issue_reference
from .commands import MacatiaError, command
from .config import RUN_BRANCH_PREFIX, MacatiaConfig, MacatiaConfigError, load_config
from .github import GitHub
from .loop_core import (
    CHILD_BASE_MARKER_PREFIX,
    DEFAULT_CONFIG_PATH,
    FINAL_REVIEW_EVIDENCE_PREFIX,
    HANDOFF_MARKER,
    ORIGINAL_EVIDENCE_PREFIX,
    RESET_MARKER_PREFIX,
    REVIEWED_RUN,
    STANDARD_RUN,
    author_login,
    branch_heads,
    cumulative_draft_pull_request_title,
    implementation_evidence_heads,
    labels,
    latest_implementation_heads,
    positive_integer,
)
from .progress import spinner


# The lifecycle states a reset makes ready for an agent again, as label roles; a child
# carrying several left the first one listed.
RESET_STATES = ("human_review", "agent_working", "implemented")
RUN_BRANCH_NAME = re.compile(re.escape(RUN_BRANCH_PREFIX) + r"([1-9][0-9]*)")
# Persisted in the comment a reset closes a cumulative draft PR with, so its value must
# never change: a rerun reads from it what an interrupted reset closed and deleted.
CLOSING_MARKER_PREFIX = "<!-- macatia-reset-closing: "
# Every marker Macatia puts in its issue comments; the legacy implementation evidence carries
# none and is recognised by its prose.
MACATIA_MARKERS = (
    ORIGINAL_EVIDENCE_PREFIX,
    STANDARD_RUN.implementation_evidence_prefix,
    CHILD_BASE_MARKER_PREFIX,
    FINAL_REVIEW_EVIDENCE_PREFIX,
    # Final review handoff comments carry it too.
    HANDOFF_MARKER,
    *(marker for mode in (STANDARD_RUN, REVIEWED_RUN)
      for marker in (mode.parent_completion_marker, mode.terminal_summary_marker)),
    RESET_MARKER_PREFIX,
    CLOSURE_MARKER,
)


class ResetRefused(MacatiaError):
    """A check failed, or no one confirmed: nothing was written."""


class ResetCancelled(Exception):
    """Ctrl-C at the picker or the confirmation: nothing was written."""


@dataclass(frozen=True)
class ResettableRunTarget:
    """A resettable run target as the resettable run target picker offers it."""

    number: int
    title: str
    # The children a reset of a parent issue makes ready for an agent; None for a standalone issue.
    children: int | None

    @property
    def label(self) -> str:
        kind = "standalone" if self.children is None else f"parent · {self.children} children to reset"
        return f"#{self.number}  {self.title}  ({kind})"


@dataclass(frozen=True)
class RunTraces:
    """The run branches and open cumulative draft PRs of every run target, each found with one
    listing, as the picker reads them before inspecting each run target they point to."""

    # Run branch heads by run target number, on the configured remote and in the checkout.
    remote_heads: dict[int, str]
    local_heads: dict[int, str]
    # The open cumulative draft PRs by run target number, as the pull request lookup lists them.
    pulls: dict[int, list[dict[str, Any]]]
    checked_out: str | None
    # The run worktrees by run branch, as `run_worktrees` lists them.
    worktrees: dict[str, RunWorktree]

    def run_branch(self, number: int) -> RunBranch | None:
        return RunBranch.found(
            number, self.remote_heads.get(number), self.local_heads.get(number), self.checked_out,
            self.worktrees,
        )


@dataclass(frozen=True)
class ChildReset:
    """A child the reset makes ready for an agent again, or whose Macatia comments a purge deletes."""

    issue: dict[str, Any]
    # The label role of the lifecycle state the child leaves; None for a child a purge
    # touches only for its Macatia comments.
    previous_state: str | None
    # Whether an interrupted reset already posted its reset marker.
    marked: bool


@dataclass(frozen=True)
class RunWorktree:
    """The checkout of a run branch other than the main one, its directory deleted or not."""

    path: pathlib.Path
    # Whether Git keeps it locked: `git worktree remove` refuses it.
    locked: bool


@dataclass(frozen=True)
class RunBranch:
    """The run target's run branch, where it still exists."""

    name: str
    # Its head on the configured remote and in the checkout, None where it is gone.
    remote_head: str | None
    local_head: str | None
    # Whether the checkout has it checked out: the reset switches to the base branch first.
    checked_out: bool
    # Its run worktree, which the reset removes first, or None without one.
    worktree: RunWorktree | None

    @staticmethod
    def found(
        number: int,
        remote_head: str | None,
        local_head: str | None,
        checked_out: str | None,
        worktrees: dict[str, RunWorktree],
    ) -> RunBranch | None:
        """The run branch of run target `number` at these heads, None when it exists nowhere;
        `checked_out` is the checkout's current branch and `worktrees` the run worktrees."""
        name = f"{RUN_BRANCH_PREFIX}{number}"
        if remote_head is None and local_head is None:
            return None
        return RunBranch(
            name, remote_head, local_head, local_head is not None and checked_out == name,
            worktrees.get(name) if local_head is not None else None,
        )

    def record(self) -> dict[str, Any]:
        """The deleted branch as the recap records it: its remote head, else its local one,
        and its local head too when that differs."""
        head = self.remote_head or self.local_head
        assert head is not None
        local = {} if self.local_head in (None, head) else {"local_head": self.local_head}
        return {"name": self.name, "head": head, **local}


@dataclass(frozen=True)
class Plan:
    """What resetting an open run target writes."""

    target: dict[str, Any]
    standalone: bool
    # The open cumulative draft PRs to close, as the pull request lookup lists them.
    pulls: list[dict[str, Any]]
    # The run branch to delete, or None when it exists nowhere.
    branch: RunBranch | None
    # The cumulative draft PRs from the run branch an interrupted reset already closed since
    # the previous recap, and the run branch it recorded deleting, read from their comments.
    already_closed: list[int]
    recorded_branch: dict[str, Any] | None
    # The children to reset now, in order; a standalone issue is its own only child.
    children: list[ChildReset]
    # The children an interrupted reset already made ready for an agent, by number, with
    # the label role of the state each left: read from their reset markers.
    already_reset: dict[int, str]
    # The run target's labels to remove; a parent issue then gains ready-for-agent.
    remove: list[str]
    # For a purge, the identifiers of the Macatia comments to delete on the run target and on
    # each child it touches, by issue number; None for a reset that leaves reset markers.
    purge: dict[int, list[Any]] | None = None


def parse_arguments(argv: list[str], prog: str, *, issue_required: bool = True) -> argparse.Namespace:
    """Read reset's arguments. Without `ISSUE`, `issue` is None, a usage error when
    `issue_required`."""
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Reset a run target so a run can start on it as though no run had happened: make "
            "ready for an agent again its children that are agent working, implemented or in human "
            "review, then the run target itself."
        ),
    )
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        default=DEFAULT_CONFIG_PATH,
        help="repository-specific Macatia TOML configuration",
    )
    parser.add_argument("--yes", action="store_true", help="reset without asking for confirmation")
    parser.add_argument(
        "--purge",
        action="store_true",
        help="delete Macatia's comments on the run target and its children instead of leaving reset markers",
    )
    parser.add_argument("issue", type=positive_integer, nargs="?", metavar="ISSUE")
    arguments = parser.parse_args(argv)  # Exits with status 2 on usage errors.
    if arguments.issue is None and issue_required:
        parser.error("pass the issue to reset: macatia reset ISSUE")
    return arguments


def reset_marker_comment(body: str) -> dict[str, Any] | None:
    """The fields a reset marker records, or None when `body` carries none. A malformed
    marker records nothing, but is still a barrier, as for the run."""
    if RESET_MARKER_PREFIX not in body:
        return None
    match = re.search(re.escape(RESET_MARKER_PREFIX) + r"(\{.*?\}) -->", body)
    try:
        marker = json.loads(match.group(1)) if match else {}
    except json.JSONDecodeError:
        marker = {}
    return marker if isinstance(marker, dict) else {}


def trusted_reset_markers(config: MacatiaConfig, issue: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """The issue's trusted reset markers, oldest first, each with its comment's index."""
    markers = []
    for index, comment in enumerate(issue.get("comments", [])):
        if author_login(comment) not in config.trusted_authors:
            continue
        marker = reset_marker_comment(str(comment.get("body", "")))
        if marker is not None:
            markers.append((index, marker))
    return markers


def is_macatia_comment(config: MacatiaConfig, comment: dict[str, Any]) -> bool:
    """Whether a purge deletes the comment: trusted, and carrying a Macatia marker."""
    if author_login(comment) not in config.trusted_authors:
        return False
    body = str(comment.get("body", ""))
    return any(marker in body for marker in MACATIA_MARKERS) or (
        implementation_evidence_heads(body, STANDARD_RUN, allow_original=True) is not None
    )


def has_macatia_comment(config: MacatiaConfig, issue: dict[str, Any]) -> bool:
    return any(is_macatia_comment(config, comment) for comment in issue.get("comments", []))


def macatia_comment_ids(github: Any, config: MacatiaConfig, issue: dict[str, Any]) -> list[Any]:
    """The identifiers of the issue's comments a purge deletes, oldest first; listed only
    when its details carry one."""
    if not has_macatia_comment(config, issue):
        return []
    return [
        comment["id"] for comment in github.issue_comments(int(issue["number"]))
        if is_macatia_comment(config, comment)
    ]


def latest_recap_time(config: MacatiaConfig, target: dict[str, Any]) -> str | None:
    """When the run target's previous recap, its latest trusted reset marker, was posted."""
    markers = trusted_reset_markers(config, target)
    if not markers:
        return None
    return str(target["comments"][markers[-1][0]].get("createdAt", ""))


def pending_reset_marker(
    config: MacatiaConfig, child: dict[str, Any], since: str | None
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


def previous_state(config: MacatiaConfig, issue: dict[str, Any]) -> str | None:
    """The label role of the lifecycle state a reset makes the issue leave, or None."""
    current = labels(issue)
    return next((role for role in RESET_STATES if config.labels[role] in current), None)


def is_merged(github: Any, config: MacatiaConfig, issue: dict[str, Any]) -> str | None:
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


def git(root: pathlib.Path, *arguments: str) -> str:
    return command(root, ["git", *arguments]).stdout.strip()


def run_worktrees(root: pathlib.Path) -> dict[str, RunWorktree]:
    """The run worktrees of the repository by run branch: each checkout other than the main
    one with a run branch checked out, wherever it is."""
    worktrees = {}
    # The main checkout comes first.
    for entry in git(root, "worktree", "list", "--porcelain").split("\n\n")[1:]:
        lines = entry.splitlines()
        branch = next((line.removeprefix("branch refs/heads/") for line in lines
                       if line.startswith("branch refs/heads/")), None)
        if lines and branch is not None and RUN_BRANCH_NAME.fullmatch(branch):
            worktrees[branch] = RunWorktree(
                pathlib.Path(lines[0].removeprefix("worktree ")).resolve(),
                any(line == "locked" or line.startswith("locked ") for line in lines),
            )
    return worktrees


def find_run_branch(root: pathlib.Path, config: MacatiaConfig, number: int) -> RunBranch | None:
    """The run branch of run target `number` on the configured remote and in the checkout,
    with its run worktree."""
    name = f"{RUN_BRANCH_PREFIX}{number}"
    try:
        remote_head, local_head = branch_heads(root, config.remote, name)
    except MacatiaError as error:
        raise MacatiaError(f"Cannot look for {name}, nothing reset: {error}") from error
    return RunBranch.found(number, remote_head, local_head, current_branch(root), run_worktrees(root))


def open_cumulative_draft_pull_requests(
    github: Any, config: MacatiaConfig, numbers: list[int]
) -> dict[int, list[dict[str, Any]]]:
    """The open pull requests into the base branch titled as Macatia titles the cumulative
    draft PR of one of the run targets `numbers`, by run target, found with one lookup."""
    titles = {
        cumulative_draft_pull_request_title(number, standalone=standalone): number
        for number in numbers
        for standalone in (False, True)
    }
    pulls = github.open_pull_requests_by_title(config.base_branch, set(titles))
    if not isinstance(pulls, list):
        raise MacatiaError("GitHub pull request lookup returned invalid data")
    by_target: dict[int, list[dict[str, Any]]] = {}
    for pull in sorted(pulls, key=lambda pull: int(pull["number"])):
        by_target.setdefault(titles[pull["title"]], []).append(pull)
    return by_target


def closing_marker(body: str) -> dict[str, Any] | None:
    """The fields the closing marker of a cumulative draft PR's comment records, or None."""
    match = re.search(re.escape(CLOSING_MARKER_PREFIX) + r"(\{.*?\}) -->", body)
    try:
        marker = json.loads(match.group(1)) if match else None
    except json.JSONDecodeError:
        return None
    return marker if isinstance(marker, dict) else None


def closed_by_interrupted_reset(
    github: Any, config: MacatiaConfig, number: int, since: str | None
) -> tuple[list[int], dict[str, Any] | None]:
    """The cumulative draft PRs from the run branch a reset closed since the run target's
    previous recap, and the run branch it recorded deleting, read from its closing comments."""
    closed, branch = set(), None
    for pull in github.closed_pull_requests(f"{RUN_BRANCH_PREFIX}{number}", config.base_branch):
        for comment in github.issue_comments(int(pull["number"])):
            if author_login(comment) not in config.trusted_authors:
                continue
            if since is not None and not str(comment.get("createdAt", "")) > since:
                continue
            marker = closing_marker(str(comment.get("body", "")))
            if marker is None or marker.get("run_target") != number:
                continue
            closed.add(int(pull["number"]))
            if isinstance(marker.get("deleted_branch"), dict):
                branch = marker["deleted_branch"]
    return sorted(closed), branch


def inspect(
    github: Any,
    config: MacatiaConfig,
    target: dict[str, Any],
    root: pathlib.Path,
    traces: RunTraces | None = None,
    *,
    purge: bool = False,
) -> tuple[Plan, list[Failure]]:
    """The plan for resetting `target`, or purging it, and every reason it cannot be but a
    dirty worktree; its run branch and cumulative draft PRs are looked up unless `traces`
    lists them. A purge touches each child still open that is agent working, implemented, or
    in human review, or ready for an agent and carrying a Macatia comment, as a reset left it."""
    number = int(target["number"])
    failures: list[Failure] = []
    try:
        parent = github.parent(number)
    except MacatiaError as error:
        raise MacatiaError(f"Cannot read the parent of #{number}, nothing reset: {error}") from error
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
    plan = Plan(
        target=target, standalone=False, pulls=[], branch=None, already_closed=[],
        recorded_branch=None, children=[], already_reset={}, remove=[],
    )
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
                "merged into it; merged work is never reset",
            ))
        if purge:
            if not is_open(child):
                continue
            state = previous_state(config, child)
            ready = config.labels["ready_for_agent"] in labels(child)
            if state is not None or (ready and has_macatia_comment(config, child)):
                resets.append(ChildReset(child, state, marked=False))
            continue
        marked_state, marked_last = pending_reset_marker(config, child, since)
        if not is_open(child):
            # A child an interrupted reset reset, closed since, stays in the recap.
            if marked_state is not None and not standalone:
                already_reset[int(child["number"])] = marked_state
            continue
        state = previous_state(config, child)
        if state is not None:
            resets.append(ChildReset(child, state, marked_last and marked_state == state))
        elif marked_state is not None and not standalone:
            already_reset[int(child["number"])] = marked_state
    # A standalone issue's labels are reset as its own only child's.
    remove = [] if standalone else stale_labels(config, target, run_target=True)
    if traces is None:
        pulls = open_cumulative_draft_pull_requests(github, config, [number]).get(number, [])
        branch = find_run_branch(root, config, number)
    else:
        pulls, branch = traces.pulls.get(number, []), traces.run_branch(number)
    already_closed, recorded_branch = closed_by_interrupted_reset(
        github, config, number, latest_recap_time(config, target)
    )
    purged = None
    if purge:
        # A standalone issue is among its own children.
        purged = {
            int(issue["number"]): macatia_comment_ids(github, config, issue)
            for issue in [*(reset.issue for reset in resets), *([] if standalone else [target])]
        }
    # A Macatia comment left on the run target is a trace of a run for a purge: a reset marker,
    # or what an interrupted purge did not delete yet.
    if not (resets or already_reset or remove or pulls or branch or already_closed
            or (purged and any(purged.values()))):
        failures.append(Failure(
            f"#{number} is not resettable",
            (f"#{number} is not agent working, implemented or in human review" if standalone
             else f"no child is agent working, implemented or in human review, and #{number} is "
             "marked neither work started nor human review")
            + "; it has no open cumulative draft PR"
            + (f", no {RUN_BRANCH_PREFIX}{number} and no Macatia comment" if purge
               else f" and no {RUN_BRANCH_PREFIX}{number}"),
        ))
    return Plan(
        target=target, standalone=standalone, pulls=pulls, branch=branch,
        already_closed=already_closed, recorded_branch=recorded_branch, children=resets,
        already_reset=already_reset, remove=remove, purge=purged,
    ), failures


def checkout_failures(config: MacatiaConfig, plan: Plan, root: pathlib.Path) -> list[Failure]:
    """What the checkouts of the run branch stop the reset for: a run worktree it was started
    from inside, locked or dirty, which it would remove, and a dirty worktree with the run
    branch checked out, which it would delete."""
    branch = plan.branch
    if branch is None:
        return []
    failures = []
    worktree = branch.worktree
    if worktree is not None:
        path = worktree.path
        if root == path or path in root.parents:
            failures.append(Failure(
                f"the reset was started from inside the run worktree of {branch.name} at {path}",
                "run it from outside the run worktree, for instance from your own checkout: the "
                "reset removes it",
            ))
        if worktree.locked:
            failures.append(Failure(
                f"the run worktree of {branch.name} at {path} is locked",
                f"unlock it with `git worktree unlock {path}`: the reset removes it",
            ))
        # Git still lists a run worktree whose directory was deleted.
        if path.is_dir() and git(path, "status", "--porcelain"):
            failures.append(Failure(
                f"the run worktree of {branch.name} at {path} is dirty",
                f"commit or discard your changes there: the reset removes it with {branch.name}",
            ))
    # With a run worktree, the run branch is checked out only there.
    elif branch.checked_out and git(root, "status", "--porcelain"):
        failures.append(Failure(
            f"the worktree is dirty while {branch.name} is checked out",
            f"commit or stash your changes, or switch to {config.base_branch}: the reset "
            f"deletes {branch.name}",
        ))
    return failures


def run_branch_heads(listing: str) -> dict[int, str]:
    """The heads of the run branches a ref listing names, by run target number; each line
    holds a head, then a ref."""
    heads = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        match = RUN_BRANCH_NAME.fullmatch(parts[1].removeprefix("refs/heads/"))
        if match:
            heads[int(match.group(1))] = parts[0]
    return heads


def run_traces(
    github: Any, config: MacatiaConfig, root: pathlib.Path, open_issues: list[int]
) -> RunTraces:
    """The run branches on the configured remote, found with one heads listing, and in the
    checkout, and the open cumulative draft PRs of the `open_issues`, found with one lookup."""
    pattern = f"refs/heads/{RUN_BRANCH_PREFIX}*"
    listing = command(root, ["git", "ls-remote", "--heads", config.remote, pattern], check=False)
    if listing.returncode != 0:
        detail = " ".join((listing.stderr or "").split())
        raise MacatiaError(
            f"Cannot look for run branches on {config.remote}, nothing reset: {detail}"
        )
    local = git(root, "for-each-ref", "--format=%(objectname) %(refname)", pattern)
    return RunTraces(
        run_branch_heads(listing.stdout), run_branch_heads(local),
        open_cumulative_draft_pull_requests(github, config, open_issues), current_branch(root),
        run_worktrees(root),
    )


def resettable_run_targets(
    github: Any, config: MacatiaConfig, root: pathlib.Path, *, purge: bool = False
) -> list[ResettableRunTarget]:
    """The resettable run targets of the repository, by ascending number.

    Open issues are first narrowed to those a trace of a run points to, through a run branch,
    a cumulative draft PR or their listing's labels: an issue marked work started or human
    review, the parent of an issue agent working, implemented, or in human review, else that
    issue itself; for a purge, also a trusted issue whose details carry a Macatia comment,
    such as a reset marker. Each is then inspected as a reset of it would be. A failed lookup raises.
    """
    issues = {int(issue["number"]): issue for issue in github.open_issues() if is_open(issue)}
    traces = run_traces(github, config, root, sorted(issues))
    traced = {*traces.remote_heads, *traces.local_heads, *traces.pulls}
    run_target_labels = {config.labels["work_started"], config.labels["human_review"]}
    for number, issue in issues.items():
        if labels(issue) & run_target_labels:
            traced.add(number)
        elif previous_state(config, issue) is not None:
            parent = github.parent(number)
            traced.add(number if parent is None else int(parent["number"]))
    trusted = {number for number, issue in issues.items() if author_login(issue) in config.trusted_authors}
    # The listing carries no comments: the evidence and reset markers are in the details.
    details: dict[int, dict[str, Any]] = {}
    if purge:
        for number in sorted(trusted - traced):
            details[number] = github.issue(number)
            if has_macatia_comment(config, details[number]):
                traced.add(number)
    targets = []
    for number in sorted(traced & trusted):
        issue = details[number] if number in details else github.issue(number)
        plan, failures = inspect(github, config, issue, root, traces, purge=purge)
        if not failures:
            targets.append(ResettableRunTarget(
                number,
                str(issues[number].get("title", "")),
                None if plan.standalone else len(plan.children),
            ))
    return targets


def choose_reset_target(
    github: Any,
    config: MacatiaConfig,
    choose: Callable[[list[ResettableRunTarget], int | None], int],
    root: pathlib.Path,
    branch: str | None,
    *,
    purge: bool = False,
) -> int | None:
    """The run target the person at the terminal picks among the resettable run targets, or
    None when there is none to offer. On the run branch of a listed issue, the picker
    preselects that issue, and otherwise its first entry. Ctrl-C in the picker cancels."""
    with spinner("Reading the resettable run targets…"):
        try:
            targets = resettable_run_targets(github, config, root, purge=purge)
        except KeyboardInterrupt:
            raise ResetCancelled from None
    if not targets:
        return None
    numbers = {target.number for target in targets}
    preselected = next(
        (number for number in numbers if branch == f"{RUN_BRANCH_PREFIX}{number}"), None
    )
    try:
        chosen = choose(targets, preselected)
    except KeyboardInterrupt:
        raise ResetCancelled from None
    if chosen not in numbers:
        raise MacatiaError(f"#{chosen} is not one of the resettable run targets")
    return chosen


def stale_labels(config: MacatiaConfig, issue: dict[str, Any], *, run_target: bool) -> list[str]:
    """The labels a reset removes from an issue: its lifecycle labels other than
    ready-for-agent, and work started on a run target."""
    stale = config.lifecycle_labels | ({config.labels["work_started"]} if run_target else set())
    return sorted(labels(issue) & stale - {config.labels["ready_for_agent"]})


def child_marker(config: MacatiaConfig, run_target: int, state: str) -> str:
    marker = json.dumps({"previous_state": state}, separators=(",", ":"))
    return (
        f"{RESET_MARKER_PREFIX}{marker} -->\n"
        f"A reset of #{run_target} made this child ready for an agent again; it was "
        f"`{config.labels[state]}`. Macatia reads none of its records above this comment."
    )


def deleted_branch(plan: Plan) -> dict[str, Any] | None:
    """The run branch the reset deletes, or the one an interrupted reset recorded deleting."""
    return plan.branch.record() if plan.branch is not None else plan.recorded_branch


def closed_and_deleted(plan: Plan) -> tuple[dict[str, Any], str]:
    """The recap's fields for the closed cumulative draft PRs and the deleted run branch, and
    the sentence saying so; both empty when the reset closed and deleted nothing."""
    fields: dict[str, Any] = {}
    done = []
    closed = sorted({*plan.already_closed, *(int(pull["number"]) for pull in plan.pulls)})
    if closed:
        fields["closed_pull_requests"] = closed
        done.append("closed " + ", ".join(f"PR #{number}" for number in closed))
    branch = deleted_branch(plan)
    if branch is not None:
        fields["deleted_branch"] = branch
        text = f"deleted `{branch.get('name')}` at `{branch.get('head')}`"
        if "local_head" in branch:
            text += f" (`{branch['local_head']}` in the checkout)"
        done.append(text)
    return fields, (f"It {' and '.join(done)}. " if done else "")


def standalone_recap(config: MacatiaConfig, plan: Plan, state: str) -> str:
    fields, sentence = closed_and_deleted(plan)
    marker = json.dumps({"previous_state": state, **fields}, separators=(",", ":"))
    return (
        f"{RESET_MARKER_PREFIX}{marker} -->\n"
        f"Macatia reset this standalone issue so a run can start on it as though no run had "
        f"happened: it was `{config.labels[state]}` and is ready for an agent again. "
        f"{sentence}Macatia reads none of its records above this comment."
    )


def parent_recap(config: MacatiaConfig, plan: Plan, reset: dict[int, str]) -> str:
    children = [{"number": number, "previous_state": state} for number, state in sorted(reset.items())]
    fields, sentence = closed_and_deleted(plan)
    marker = json.dumps({"children": children, **fields}, separators=(",", ":"))
    listing = (
        "It made these children ready for an agent again:\n\n" + "\n".join(
            f"- #{number}, was `{config.labels[state]}`" for number, state in sorted(reset.items())
        )
        if reset else "No child needed a reset."
    )
    return (
        f"{RESET_MARKER_PREFIX}{marker} -->\n"
        "Macatia reset this parent issue so a run can start on it as though no run had happened. "
        f"{sentence}{listing}\n\nMacatia reads none of its records above this comment."
    )


def closing_comment(plan: Plan, pull: dict[str, Any]) -> str:
    number = int(plan.target["number"])
    branch = deleted_branch(plan)
    marker = json.dumps(
        {"run_target": number, **({"deleted_branch": branch} if branch is not None else {})},
        separators=(",", ":"),
    )
    kept = pull["head"] != f"{RUN_BRANCH_PREFIX}{number}"
    return (
        f"{CLOSING_MARKER_PREFIX}{marker} -->\n"
        f"A reset of #{number} closed this cumulative draft PR: a run on #{number} starts again "
        "from scratch. "
        + (f"Its branch `{pull['head']}` is kept." if kept
           else f"Its branch `{pull['head']}` is deleted and can be restored from this pull request.")
    )


def deleting(plan: Plan, number: int) -> str:
    count = len((plan.purge or {}).get(number, []))
    return f"deleting {count} Macatia {'comment' if count == 1 else 'comments'}"


def print_plan(config: MacatiaConfig, plan: Plan) -> None:
    ready = config.labels["ready_for_agent"]
    number = int(plan.target["number"])
    working = config.labels["agent_working"]
    purge = plan.purge is not None
    print(
        f"📋 Resetting {'and purging ' if purge else ''}{'standalone issue ' if plan.standalone else ''}"
        f"{issue_reference(plan.target)}:"
    )
    for closed in plan.already_closed:
        print(f"   ✓ PR #{closed}: already closed by this reset")
    for pull in plan.pulls:
        print(f"   → close PR #{pull['number']} {pull['title']} from {pull['head']}, with a comment")
    branch = plan.branch
    if branch is not None and branch.worktree is not None:
        print(
            f"   → remove the run worktree of {branch.name} at {branch.worktree.path}, with its "
            "ignored files"
        )
    if branch is not None:
        places = [
            *([f"on {config.remote} at {branch.remote_head}"] if branch.remote_head else []),
            *([f"in this checkout at {branch.local_head}"] if branch.local_head else []),
        ]
        print(
            f"   → delete {branch.name} {', then '.join(places)}"
            + (f", switching to {config.base_branch} first" if branch.checked_out else "")
        )
    if plan.standalone:
        change = ", ".join([
            *(f"-{name}" for name in stale_labels(config, plan.target, run_target=True)), f"+{ready}"
        ])
        print(f"   → {change}, {deleting(plan, number) if purge else 'with its recap reset marker'}")
    else:
        for reset in plan.children:
            change = (
                f"{', '.join(sorted(labels(reset.issue) & config.lifecycle_labels))} → {ready}"
                if reset.previous_state is not None else "labels unchanged"
            )
            print(
                f"   → {issue_reference(reset.issue)}: {change}, "
                + (deleting(plan, int(reset.issue["number"])) if purge else "with a reset marker")
            )
        for child, state in sorted(plan.already_reset.items()):
            print(f"   ✓ #{child}: already reset (was {config.labels[state]})")
        change = ", ".join([
            *(f"-{name}" for name in plan.remove),
            *([] if ready in labels(plan.target) else [f"+{ready}"]),
        ])
        print(
            f"   → then #{number}: {change or 'labels unchanged'}, then "
            + (deleting(plan, number) if purge else "the recap reset marker")
        )
    run_branch = f"{RUN_BRANCH_PREFIX}{number}"
    for pull in plan.pulls:
        if pull["head"] != run_branch:
            print(
                f"⚠️  PR #{pull['number']} comes from {pull['head']}: it is closed, but {pull['head']} "
                f"is kept; a reset deletes only {run_branch}"
            )
    restorable = plan.already_closed or any(pull["head"] == run_branch for pull in plan.pulls)
    if branch is not None and not restorable:
        print(
            f"⚠️  {branch.name} has no open pull request to restore it from: once deleted, only "
            + ("your reflog" if purge else "its head SHA in the recap and your reflog")
            + " can recover its commits"
        )
    for reset in plan.children:
        if working in labels(reset.issue):
            print(
                f"⚠️  #{reset.issue['number']} is {working}: a run may still be working on it; "
                "stop that run before confirming"
            )


def reset_target(
    github: Any,
    config: MacatiaConfig,
    number: int,
    *,
    yes: bool,
    confirm: Callable[[str], bool] | None,
    rerun: str,
    root: pathlib.Path,
    purge: bool = False,
) -> None:
    """Check everything, confirm, then close the cumulative draft PR, delete the run branch,
    reset the children and, last, the run target; a purge deletes the Macatia comments of each
    issue where a reset posts its reset marker."""
    with spinner(f"Checking #{number} and any sub-issues before resetting…"):
        try:
            target = github.issue(number)
        except MacatiaError as error:
            raise MacatiaError(f"Cannot read #{number}, nothing reset: {error}") from error
        if int(target.get("number", 0)) != number:
            raise MacatiaError(f"GitHub returned the wrong issue for #{number}")
        try:
            plan, failures = inspect(github, config, target, root, purge=purge)
            failures += checkout_failures(config, plan, root)
        except KeyboardInterrupt:
            raise ResetCancelled from None
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
            confirmed = confirm(f"Reset and purge #{number}?" if purge else f"Reset #{number}?")
        except KeyboardInterrupt:
            raise ResetCancelled from None
        if not confirmed:
            raise ResetRefused("Declined. Nothing reset.")
    ready = config.labels["ready_for_agent"]
    try:
        # Before any label: a rerun finds what is left through the PR lookup and the branch.
        for pull in plan.pulls:
            github.close_pull_request(int(pull["number"]), closing_comment(plan, pull))
            print(f"   ✓ closed PR #{pull['number']}")
        branch = plan.branch
        if branch is not None and branch.worktree is not None:
            # Before the run branch, which Git does not delete while it is checked out there.
            git(root, "worktree", "remove", str(branch.worktree.path))
            print(f"   ✓ removed the run worktree of {branch.name} at {branch.worktree.path}")
        if branch is not None:
            if branch.remote_head is not None:
                git(root, "push", config.remote, "--delete", branch.name)
            if branch.local_head is not None:
                if branch.checked_out:
                    git(root, "switch", config.base_branch)
                git(root, "branch", "--delete", "--force", branch.name)
            print(f"   ✓ deleted {branch.name} at {branch.record()['head']}")
        reset = dict(plan.already_reset)
        for child in plan.children:
            child_number = int(child.issue["number"])
            # The marker comes first: a rerun after it reads the state the child left there.
            # That holds for a standalone issue's recap too, else a rerun could not tell it
            # from an issue no run touched. A purge deletes the comments there instead: a rerun
            # finds a child still labelled and deletes what is left.
            if plan.purge is not None:
                for comment_id in plan.purge[child_number]:
                    github.delete_issue_comment(comment_id)
            elif not child.marked:
                # Only a purge touches a child for its comments alone.
                assert child.previous_state is not None
                github.comment_on_issue(
                    child_number,
                    standalone_recap(config, plan, child.previous_state) if plan.standalone
                    else child_marker(config, number, child.previous_state),
                )
            if child.previous_state is not None:
                github.edit_issue_labels(
                    child_number,
                    add=ready,
                    remove=stale_labels(config, child.issue, run_target=plan.standalone),
                )
                reset[child_number] = child.previous_state
            if not plan.standalone:
                print(f"   ✓ #{child_number}")
        if not plan.standalone:
            if plan.remove or ready not in labels(plan.target):
                github.edit_issue_labels(number, add=ready, remove=plan.remove)
            if plan.purge is not None:
                # A rerun finds what is left: a Macatia comment on the run target is a trace.
                for comment_id in plan.purge[number]:
                    github.delete_issue_comment(comment_id)
            else:
                # The recap comes last: its presence means the reset finished.
                github.comment_on_issue(number, parent_recap(config, plan, reset))
    except MacatiaError as error:
        raise MacatiaError(f"{error}\nRerun `{rerun}` to finish the reset.") from error
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
    choose_run_target: Callable[[list[ResettableRunTarget], int | None], int] | None = None,
    branch: Callable[[pathlib.Path], str | None] = current_branch,
    repository_root: pathlib.Path | None = None,
    prog: str = "macatia reset",
) -> int:
    """Run `macatia reset`; `confirm` asks a Yes/No question and `choose_run_target` picks
    among the resettable run targets (by ascending number, preselected number or None), both
    None without a terminal. Only the picker lets reset start without `ISSUE`; `branch` reads
    the checked-out branch it preselects from.

    Exits 0 once reset or with no resettable run target, 1 on a refusal, a declined
    confirmation or a GitHub error, 2 on a usage error, 130 on Ctrl-C.
    """
    arguments = parse_arguments(argv, prog, issue_required=choose_run_target is None)
    root = (repository_root or pathlib.Path.cwd()).resolve()
    config_path = arguments.config if arguments.config.is_absolute() else root / arguments.config

    def rerun_command(issue: int | None) -> str:
        return shlex.join([
            *prog.split(),
            *(["--config", str(arguments.config)] if arguments.config != DEFAULT_CONFIG_PATH else []),
            *(["--purge"] if arguments.purge else []),
            *([str(issue)] if issue is not None else []),
        ])

    rerun = rerun_command(arguments.issue)
    try:
        config = load_config(config_path.resolve(), root, require_review=False)
        access = github(root, config.repository)
        issue = arguments.issue
        if issue is None:
            assert choose_run_target is not None
            issue = choose_reset_target(
                access, config, choose_run_target, root, branch(root), purge=arguments.purge
            )
            if issue is None:
                print(f"No run target in {config.repository} is resettable.")
                return 0
            rerun = rerun_command(issue)
        reset_target(
            access,
            config,
            issue,
            yes=arguments.yes,
            confirm=confirm,
            rerun=rerun,
            root=root,
            purge=arguments.purge,
        )
    except ResetCancelled:
        print("\nCancelled. Nothing reset.", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        print(f"\nInterrupted. Rerun `{rerun}` to finish the reset.", file=sys.stderr)
        return 130
    except (MacatiaError, MacatiaConfigError) as error:
        print(f"❌ {error}", file=sys.stderr)
        return 1
    return 0
