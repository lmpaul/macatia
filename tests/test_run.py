"""In-memory run tests: the whole loop against a GitHub fake, scripted sessions, and real Git."""

import json
import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from ralph_kit import loop, loop_review
from run_support import (
    BRANCH,
    FakeGitHub,
    RunRepository,
    ScriptedSessions,
    Session,
    start_run,
)


PARENT = 191
CLOSED_BLOCKER = 192
CHILD = 193


class SingleChildHappyPathTests(unittest.TestCase):
    def setUp(self):
        self.repository = RunRepository(self)
        self.github = FakeGitHub()
        self.github.add_issue(PARENT)
        self.github.add_issue(CLOSED_BLOCKER, parent=PARENT, state="closed")
        self.github.add_issue(
            CHILD,
            parent=PARENT,
            blocked_by=[CLOSED_BLOCKER],
            comments=[{"author": {"login": "lmpaul"}, "body": "Owner requirement context."}],
        )

    def assert_child_published(self, result, *, commit_count):
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertIn(
            f"Handled issue #{PARENT}: all 2 children complete; "
            f"{commit_count} agent sessions and 0 infrastructure retries this run.",
            result.stdout,
        )
        base_sha = self.repository.base_sha
        commits = self.repository.commits_since(base_sha)
        self.assertEqual(len(commits), commit_count)
        head = self.repository.head()
        self.assertTrue(self.repository.is_clean())
        self.assertEqual(self.repository.pushed_head(), head)

        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        evidence = self.github.comments(CHILD)[-1]
        self.assertIn(f"Ralph verified implementation commits `{base_sha}..{commits[0]}`", evidence)
        self.assertIn(f"Applicable local gates and canonical CI are green for exact head `{head}`", evidence)

        pull = self.github.pull_request()
        self.assertEqual((pull.base, pull.head), ("main", BRANCH))
        self.assertFalse(pull.is_draft)
        self.assertIn(f"Ralph summary: parent #{PARENT};", pull.comments[-1])
        self.assertIn("implemented children this run: 1;", pull.comments[-1])

        self.assertEqual(self.github.labels(PARENT), {"human-review"})
        parent_comments = self.github.comments(PARENT)
        self.assertIn("all 2 children complete", parent_comments[-2])
        self.assertIn(f"cumulative head `{head}`", parent_comments[-2])
        self.assertIn("final outcome: completed.", parent_comments[-1])

        timings = json.loads((self.repository.diagnostics() / "timings.json").read_text())
        self.assertTrue(
            {"local gates", "GitHub Actions wait"} <= {phase["label"] for phase in timings["phases"]}
        )
        return commits, evidence

    def test_standard_run_publishes_the_verified_implementation(self):
        sessions = ScriptedSessions(self.repository, Session("committed"))

        result = start_run(loop.MODE, self.repository, self.github, sessions, PARENT)

        commits, evidence = self.assert_child_published(result, commit_count=1)
        self.assertIn("ralph-experimental-implementation-evidence", evidence)
        self.assertIn("No separate Ralph review session was run.", evidence)
        self.assertIn(f"verified head `{commits[0]}` with no corrections", evidence)
        self.assertIn("<!-- ralph-experimental-parent-completion -->", self.github.comments(PARENT)[-2])

    def test_reviewed_run_publishes_the_reviewed_correction(self):
        sessions = ScriptedSessions(
            self.repository,
            Session("committed"),
            Session("committed", "Reviewed the exact diff and committed one correction."),
        )

        result = start_run(loop_review.MODE, self.repository, self.github, sessions, PARENT, 4)

        commits, evidence = self.assert_child_published(result, commit_count=2)
        self.assertIn("<!-- ralph-implementation-evidence: ", evidence)
        self.assertIn(f"corrective review commits `{commits[0]}..{commits[1]}`", evidence)
        self.assertIn("<!-- ralph-parent-completion -->", self.github.comments(PARENT)[-2])


if __name__ == "__main__":
    unittest.main()
