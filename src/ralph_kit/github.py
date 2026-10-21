"""GitHub adapter: every GitHub call a run makes, through the `gh` CLI."""

from __future__ import annotations

import json
import pathlib
import subprocess
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .commands import RalphError, command


def flatten_pages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise RalphError("GitHub list endpoint returned a non-list response")
    if value and isinstance(value[0], list):
        return [item for page in value for item in page]
    return value


# The statuses of GitHub's compare API for `base...head`.
COMPARE_STATUSES = {"ahead", "behind", "identical", "diverged"}


@dataclass(frozen=True)
class MergedPullRequest:
    number: int
    merge_commit: str


def merged_pull_requests_into(base: str, pulls: list[dict[str, Any]]) -> list[MergedPullRequest]:
    """The pull requests of a commit-to-pull-requests listing that were merged into `base`."""
    merged = []
    for pull in pulls:
        if pull.get("merged_at") is None or (pull.get("base") or {}).get("ref") != base:
            continue
        number, merge_commit = pull.get("number"), pull.get("merge_commit_sha")
        if not isinstance(number, int) or not isinstance(merge_commit, str) or not merge_commit:
            raise RalphError(f"GitHub listed a merged pull request without a number or merge commit: {pull!r}")
        merged.append(MergedPullRequest(number, merge_commit))
    return merged


# The CI diagnostic records these commands as evidence, so the in-memory GitHub
# of the run tests reports the same ones.


def check_run_command(repository: str, check_id: Any) -> list[str]:
    return ["gh", "api", f"repos/{repository}/check-runs/{check_id}"]


def actions_job_command(repository: str, job_id: str) -> list[str]:
    return ["gh", "api", f"repos/{repository}/actions/jobs/{job_id}"]


def job_log_command(repository: str, job_id: str) -> list[str]:
    return ["gh", "run", "view", "--repo", repository, "--job", job_id, "--log"]


class GitHub:
    """Runs `gh` from the repository root against the configured repository."""

    def __init__(self, repository_root: pathlib.Path, repository: str):
        self.repository_root = repository_root
        self.repository = repository

    def _gh(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return command(self.repository_root, ["gh", *arguments], check=check)

    def _json(self, *arguments: str) -> Any:
        output = self._gh(*arguments).stdout
        try:
            return json.loads(output)
        except json.JSONDecodeError as error:
            raise RalphError(
                f"GitHub returned invalid JSON for {' '.join(arguments)}"
            ) from error

    def _list(self, endpoint: str) -> list[dict[str, Any]]:
        return flatten_pages(self._json("api", endpoint, "--paginate", "--slurp"))

    def repository_name(self) -> Any:
        """The `nameWithOwner` of the repository `gh` resolves from the checkout."""
        return self._json("repo", "view", "--json", "nameWithOwner").get("nameWithOwner")

    def issue(self, number: int) -> Any:
        return self._json(
            "issue",
            "view",
            str(number),
            "--repo",
            self.repository,
            "--json",
            "number,title,body,state,labels,author,comments,updatedAt",
        )

    def sub_issues(self, parent: int) -> list[dict[str, Any]]:
        return self._list(f"repos/{self.repository}/issues/{parent}/sub_issues")

    def commit_is_on_branch(self, branch: str, sha: str) -> bool:
        """Whether a commit is on a remote branch, as GitHub compares them; never local Git."""
        basehead = f"{urllib.parse.quote(branch, safe='/')}...{sha}"
        status = self._json(
            "api", f"repos/{self.repository}/compare/{basehead}", "--jq", "{status}"
        ).get("status")
        if status not in COMPARE_STATUSES:
            raise RalphError(f"GitHub compared {branch} with {sha} as {status!r}")
        # `behind` or `identical`: the branch already contains the commit.
        return status in {"behind", "identical"}

    def merged_pull_requests(self, base: str, sha: str) -> list[MergedPullRequest]:
        """The pull requests merged into `base` that GitHub associates with a commit, whatever
        the merge method: a squash or rebase merge leaves the commit off `base`."""
        return merged_pull_requests_into(
            base, self._list(f"repos/{self.repository}/commits/{sha}/pulls")
        )

    def blockers(self, child: int) -> list[dict[str, Any]]:
        return self._list(
            f"repos/{self.repository}/issues/{child}/dependencies/blocked_by"
        )

    def comment_on_issue(self, number: int, body: str) -> None:
        self._gh("issue", "comment", str(number), "--repo", self.repository, "--body", body)

    def close_issue(self, number: int, reason: str) -> None:
        """Close an issue with a reason: `completed` or `not planned`."""
        self._gh("issue", "close", str(number), "--repo", self.repository, "--reason", reason)

    def edit_issue_labels(self, number: int, add: str, remove: list[str]) -> None:
        arguments = ["issue", "edit", str(number), "--repo", self.repository, "--add-label", add]
        if remove:
            arguments.extend(["--remove-label", ",".join(remove)])
        self._gh(*arguments)

    def open_pull_requests(self, head: str, base: str) -> Any:
        return self._json(
            "pr",
            "list",
            "--head",
            head,
            "--base",
            base,
            "--state",
            "open",
            "--json",
            "number,isDraft,url",
        )

    def comment_on_pull_request(self, number: int, body: str) -> None:
        self._gh("pr", "comment", str(number), "--body", body)

    def create_draft_pull_request(self, base: str, head: str, title: str, body: str) -> None:
        self._gh(
            "pr",
            "create",
            "--draft",
            "--base",
            base,
            "--head",
            head,
            "--title",
            title,
            "--body",
            body,
        )

    def mark_pull_request_draft(self, number: int) -> None:
        self._gh("pr", "ready", str(number), "--undo")

    def mark_pull_request_ready(self, number: int) -> None:
        self._gh("pr", "ready", str(number))

    def check_runs(self, head_sha: str) -> Any:
        """Raw paginated check-runs pages for an exact commit."""
        return self._json(
            "api",
            f"repos/{self.repository}/commits/{head_sha}/check-runs",
            "--paginate",
            "--slurp",
        )

    # The CI diagnostic keeps each raw command and its output as evidence, so
    # these return the completed process instead of raising on failure.

    def check_run(self, check_id: Any) -> subprocess.CompletedProcess[str]:
        return command(
            self.repository_root, check_run_command(self.repository, check_id), check=False
        )

    def actions_job(self, job_id: str) -> subprocess.CompletedProcess[str]:
        return command(
            self.repository_root, actions_job_command(self.repository, job_id), check=False
        )

    def job_log(self, job_id: str) -> subprocess.CompletedProcess[str]:
        return command(
            self.repository_root, job_log_command(self.repository, job_id), check=False
        )
