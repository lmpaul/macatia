"""The GitHub adapter's reading of `gh` answers that a run cannot take on trust."""

import json
import pathlib
import subprocess
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from ralph_kit.commands import RalphError  # noqa: E402
from ralph_kit.github import parent_from_lookup  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
