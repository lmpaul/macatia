"""The GitHub adapter's reading of `gh` answers that a run cannot take on trust."""

import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from ralph_kit.commands import RalphError  # noqa: E402
from ralph_kit.github import GitHub, parent_from_lookup  # noqa: E402


ARGUMENTS = ["gh", "api", "repos/lmpaul/ralph-kit/issues/90/parent"]
DOCUMENTATION = "https://docs.github.com/rest/issues/sub-issues#get-parent-issue"


def answer(returncode, body, stderr=""):
    stdout = body if isinstance(body, str) else json.dumps(body)
    return subprocess.CompletedProcess(ARGUMENTS, returncode, stdout, stderr)


def not_found(message):
    return answer(
        1,
        {"message": message, "documentation_url": DOCUMENTATION, "status": "404"},
        f"gh: {message} (HTTP 404)\n",
    )


class ParentLookupTests(unittest.TestCase):
    def test_a_sub_issue_has_the_parent_github_returns(self):
        parent = {"number": 89, "title": "Parent", "state": "open"}

        self.assertEqual(parent_from_lookup(answer(0, parent)), parent)

    def test_only_no_parent_issue_found_means_no_parent(self):
        self.assertIsNone(parent_from_lookup(not_found("No parent issue found")))

    def test_any_other_failure_is_an_error(self):
        for name, completed in (
            ("missing issue", not_found("Not Found")),
            ("server error", answer(
                1, {"message": "Server Error", "status": "502"}, "gh: Server Error (HTTP 502)\n"
            )),
            ("no response body", answer(1, "", "error connecting to api.github.com\n")),
            ("no parent message with another status", answer(
                1, {"message": "No parent issue found", "status": "500"}, "gh: (HTTP 500)\n"
            )),
        ):
            with self.subTest(name), self.assertRaises(RalphError) as raised:
                parent_from_lookup(completed)
            self.assertIn(" ".join(ARGUMENTS), str(raised.exception))

    def test_a_success_without_a_parent_issue_is_an_error(self):
        for body in ("not json", {"message": "ok"}, [], None):
            with self.subTest(body=body), self.assertRaises(RalphError):
                parent_from_lookup(answer(0, body))


def listed_issue(number, *, login="lmpaul", labels=("ready-for-agent",), **fields):
    """An issue as GitHub's REST issue listing returns it."""
    return {
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "labels": [{"name": label, "color": "ededed"} for label in labels],
        "user": {"login": login},
        "body": "Unread.",
        **fields,
    }


class OpenIssuesTests(unittest.TestCase):
    def open_issues(self, pages):
        github = GitHub(pathlib.Path("/repository"), "lmpaul/ralph-kit")
        with mock.patch("ralph_kit.github.command") as command:
            command.return_value = subprocess.CompletedProcess([], 0, json.dumps(pages), "")
            issues = github.open_issues()
        command.assert_called_once_with(
            pathlib.Path("/repository"),
            [
                "gh", "api", "repos/lmpaul/ralph-kit/issues?state=open&per_page=100",
                "--paginate", "--slurp",
            ],
            check=True,
        )
        return issues

    def test_every_page_of_open_issues_is_read_without_pull_requests(self):
        pages = [
            [listed_issue(12), listed_issue(13, pull_request={"url": "https://api.github.com/pulls/13"})],
            [listed_issue(14, login="someone", labels=("needs-info", "agent-working"))],
        ]

        self.assertEqual(
            self.open_issues(pages),
            [
                {
                    "number": 12, "title": "Issue 12", "state": "OPEN",
                    "labels": [{"name": "ready-for-agent"}], "author": {"login": "lmpaul"},
                },
                {
                    "number": 14, "title": "Issue 14", "state": "OPEN",
                    "labels": [{"name": "needs-info"}, {"name": "agent-working"}],
                    "author": {"login": "someone"},
                },
            ],
        )

    def test_no_open_issue_is_an_empty_list(self):
        self.assertEqual(self.open_issues([[]]), [])

    def test_an_issue_without_a_number_is_an_error(self):
        for issue in ({"title": "No number"}, listed_issue("12")):
            with self.subTest(issue=issue), self.assertRaises(RalphError):
                self.open_issues([[issue]])



def listed_pull(number, title, ref, owner="lmpaul/ralph-kit"):
    """A pull request as GitHub's REST pull request listing returns it."""
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/lmpaul/ralph-kit/pull/{number}",
        "head": {"ref": ref, "user": {"login": owner.split("/")[0]}, "repo": {"full_name": owner}},
    }


class PullRequestsByTitleTests(unittest.TestCase):
    def test_only_open_pull_requests_into_the_base_with_a_listed_title_are_read(self):
        github = GitHub(pathlib.Path("/repository"), "lmpaul/ralph-kit")
        pages = [
            [listed_pull(12, "Implement Ralph parent #69", "ralph-kit/issue-69")],
            [
                listed_pull(13, "Implement Ralph parent #690", "ralph-kit/issue-690"),
                listed_pull(14, "Implement Ralph issue #69", "ralph-kit/issue-69", owner="fork/ralph-kit"),
            ],
        ]
        with mock.patch("ralph_kit.github.command") as command:
            command.return_value = subprocess.CompletedProcess([], 0, json.dumps(pages), "")
            pulls = github.open_pull_requests_by_title(
                "main", {"Implement Ralph parent #69", "Implement Ralph issue #69"}
            )

        command.assert_called_once_with(
            pathlib.Path("/repository"),
            [
                "gh", "api", "repos/lmpaul/ralph-kit/pulls?state=open&base=main&per_page=100",
                "--paginate", "--slurp",
            ],
            check=True,
        )
        self.assertEqual(pulls, [
            {"number": 12, "title": "Implement Ralph parent #69", "head": "ralph-kit/issue-69"},
            {
                "number": 14, "title": "Implement Ralph issue #69",
                "head": "fork/ralph-kit:ralph-kit/issue-69",
            },
        ])

if __name__ == "__main__":
    unittest.main()
