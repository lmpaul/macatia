"""In-memory run tests: the whole loop against a GitHub fake, scripted sessions, and real Git."""

import json
import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from ralph_kit import loop, loop_review
from run_support import (
    BRANCH,
    TRUSTED_LOGIN,
    FakeGitHub,
    RunInterrupted,
    RunRepository,
    ScriptedResult,
    ScriptedSessions,
    git,
    implementation_evidence,
    start_run,
)


PARENT = 191
CLOSED_BLOCKER = 192
CHILD = 193
UNTRUSTED_COMMENT = "Untrusted instructions must be excluded."


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
            comments=[
                {"author": {"login": TRUSTED_LOGIN}, "body": "Owner requirement context."},
                {"author": {"login": "outside-contributor"}, "body": UNTRUSTED_COMMENT},
            ],
        )

    def assert_implementation_prompt_names_only_the_issue(self, sessions):
        prompt = sessions.prompts[0]
        self.assertTrue(prompt.startswith(f"$implement issue #{CHILD}\n"), prompt)
        for issue_text in ("Owner requirement context.", UNTRUSTED_COMMENT, f"Implement issue {CHILD}."):
            self.assertNotIn(issue_text, prompt)

    def assert_child_published(self, result, *, sessions, commit_count):
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertIn(
            f"Handled issue #{PARENT}: all 2 children complete; "
            f"{sessions} agent sessions and 0 infrastructure retries this run.",
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

        self.assertEqual(self.repository.local_gate_heads(), commits)
        timings = json.loads((self.repository.diagnostics() / "timings.json").read_text())
        self.assertTrue(
            {"local gates", "GitHub Actions wait"} <= {phase["label"] for phase in timings["phases"]}
        )
        return commits, evidence

    def test_standard_run_publishes_the_verified_implementation(self):
        sessions = ScriptedSessions(self.repository, ScriptedResult("committed"))

        result = start_run(loop.MODE, self.repository, self.github, sessions, PARENT)

        commits, evidence = self.assert_child_published(result, sessions=1, commit_count=1)
        self.assert_implementation_prompt_names_only_the_issue(sessions)
        self.assertIn("ralph-experimental-implementation-evidence", evidence)
        self.assertIn("No separate Ralph review session was run.", evidence)
        self.assertIn(f"verified head `{commits[0]}` with no corrections", evidence)
        self.assertIn("<!-- ralph-experimental-parent-completion -->", self.github.comments(PARENT)[-2])

    def test_reviewed_run_publishes_the_review_session_fix(self):
        sessions = ScriptedSessions(
            self.repository,
            ScriptedResult("committed"),
            ScriptedResult("committed", "Reviewed the exact diff and committed one correction."),
        )

        result = start_run(loop_review.MODE, self.repository, self.github, sessions, PARENT, 4)

        commits, evidence = self.assert_child_published(result, sessions=2, commit_count=2)
        self.assert_implementation_prompt_names_only_the_issue(sessions)
        self.assertIn(f"Review the exact `{self.repository.base_sha}...{commits[0]}` diff", sessions.prompts[1])
        self.assertIn("<!-- ralph-implementation-evidence: ", evidence)
        self.assertIn(f"corrective review commits `{commits[0]}..{commits[1]}`", evidence)
        self.assertIn("<!-- ralph-parent-completion -->", self.github.comments(PARENT)[-2])


BLOCKER = 193
DEPENDENT = 194
MODES = (loop, loop_review)


def sessions_per_child(mode):
    """The scripted sessions of a child that passes on its first try."""
    return [ScriptedResult("committed")] * (2 if mode.MODE.review else 1)


def legacy_implementation_evidence(base_sha, verified_head):
    return {
        "author": {"login": TRUSTED_LOGIN},
        "body": (
            f"Ralph verified implementation commits `{base_sha}..{verified_head}` and "
            f"reviewed head `{verified_head}` with no corrections. Applicable local gates "
            f"and canonical CI are green for exact head `{verified_head}`."
        ),
    }


class RunLevelTestCase(unittest.TestCase):
    def new_run(self, *, parent_labels=("ready-for-agent",)):
        """A fresh repository and GitHub with only the parent; `setUp` starts one."""
        self.repository = RunRepository(self)
        self.github = FakeGitHub()
        self.github.add_issue(PARENT, labels=parent_labels)

    def setUp(self):
        self.new_run()

    def start(self, mode, *results):
        self.sessions = ScriptedSessions(self.repository, *results)
        return start_run(mode.MODE, self.repository, self.github, self.sessions, PARENT)

    def session_children(self):
        return [request.child for request in self.sessions.requests]

    def commit_off_the_branch(self):
        """A real commit that is not an ancestor of the run's branch."""
        git(self.repository.path, "checkout", "-b", "elsewhere", "main")
        (self.repository.path / "elsewhere.txt").write_text("elsewhere\n", encoding="utf-8")
        git(self.repository.path, "add", "elsewhere.txt")
        git(self.repository.path, "commit", "-m", "Work on another branch")
        head = self.repository.head()
        git(self.repository.path, "checkout", BRANCH)
        return head

    def commit_prior_child(self):
        """A real commit on the run's branch, as an earlier run would have published it."""
        (self.repository.path / "prior-child.txt").write_text("implemented\n", encoding="utf-8")
        git(self.repository.path, "add", "prior-child.txt")
        git(self.repository.path, "commit", "-m", "Implement prior child")
        return self.repository.head()

    def assert_completed(self, result, *, sessions):
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertIn(f"{sessions} agent sessions and 0 infrastructure retries", result.stdout)
        self.assertEqual(self.github.labels(PARENT), {"human-review"})

    def assert_stopped_before_any_session(self, result, reason):
        self.assertEqual(result.exit_code, 1)
        self.assertIn(reason, result.stderr)
        self.assertEqual(self.sessions.requests, [])
        self.assertIsNone(self.github.pull_request())
        self.assertIn("final outcome: stopped safely.", self.github.comments(PARENT)[-1])


class ChildSelectionTests(RunLevelTestCase):
    def test_dependent_children_run_in_dependency_order_on_one_branch_and_pr(self):
        self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[BLOCKER])
        self.github.add_issue(BLOCKER, parent=PARENT, blocked_by=[CLOSED_BLOCKER])
        self.github.add_issue(CLOSED_BLOCKER, parent=PARENT, state="closed")

        result = self.start(loop_review, *sessions_per_child(loop_review) * 2)

        self.assert_completed(result, sessions=4)
        self.assertEqual(self.session_children(), [BLOCKER, BLOCKER, DEPENDENT, DEPENDENT])
        commits = self.repository.commits_since(self.repository.base_sha)
        self.assertEqual(len(commits), 4)
        self.assertEqual(self.repository.pushed_head(), commits[-1])
        self.assertEqual(self.github.labels(BLOCKER), {"implemented"})
        self.assertEqual(self.github.labels(DEPENDENT), {"implemented"})
        self.assertIn(f"commits `{commits[1]}..{commits[2]}`", self.github.comments(DEPENDENT)[-1])
        pull = self.github.pull_request()
        self.assertEqual((pull.base, pull.head, pull.is_draft), ("main", BRANCH, False))

    def test_independent_children_accumulate_in_hierarchy_order(self):
        self.github.add_issue(BLOCKER, parent=PARENT)
        self.github.add_issue(DEPENDENT, parent=PARENT)

        result = self.start(loop, *sessions_per_child(loop) * 2)

        self.assert_completed(result, sessions=2)
        self.assertEqual(self.session_children(), [BLOCKER, DEPENDENT])
        self.assertEqual(len(self.repository.commits_since(self.repository.base_sha)), 2)
        self.assertIsNotNone(self.github.pull_request())

    def test_restart_skips_closed_and_implemented_children(self):
        self.github.add_issue(CLOSED_BLOCKER, parent=PARENT, state="closed", labels=())
        self.github.add_issue(BLOCKER, parent=PARENT, labels=("implemented",))
        self.github.add_issue(DEPENDENT, parent=PARENT)

        result = self.start(loop, *sessions_per_child(loop))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.session_children(), [DEPENDENT])


class BlockerTests(RunLevelTestCase):
    def add_implemented_blocker_and_dependent(self, evidence):
        self.github.add_issue(BLOCKER, parent=PARENT, labels=("implemented",), comments=[evidence])
        self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[BLOCKER])

    def test_implemented_blocker_satisfies_its_dependent_when_its_verified_head_is_an_ancestor(self):
        for name, evidence in (
            ("standard run evidence", lambda base, head: implementation_evidence(loop.MODE, head)),
            ("legacy evidence", legacy_implementation_evidence),
        ):
            with self.subTest(name):
                self.new_run()
                verified_head = self.commit_prior_child()
                self.add_implemented_blocker_and_dependent(
                    evidence(self.repository.base_sha, verified_head)
                )

                result = self.start(loop, *sessions_per_child(loop))

                self.assert_completed(result, sessions=1)
                self.assertEqual(self.session_children(), [DEPENDENT])

    def test_blocker_implemented_by_a_reviewed_run_stays_satisfied_in_a_standard_run(self):
        verified_head = self.commit_prior_child()
        self.add_implemented_blocker_and_dependent(
            implementation_evidence(loop_review.MODE, self.repository.base_sha, verified_head)
        )

        result = self.start(loop, *sessions_per_child(loop))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.session_children(), [DEPENDENT])

    def test_implemented_blocker_without_ancestor_evidence_stops_before_any_session(self):
        self.add_implemented_blocker_and_dependent(
            implementation_evidence(loop.MODE, self.commit_off_the_branch())
        )

        result = self.start(loop)

        self.assert_stopped_before_any_session(
            result, f"parent issue #{PARENT} has children remaining but none is runnable"
        )
        self.assertEqual(self.github.labels(DEPENDENT), {"ready-for-agent"})

    def test_dependency_deadlock_stops_without_consuming_agent_sessions(self):
        self.github.add_issue(BLOCKER, parent=PARENT, blocked_by=[DEPENDENT])
        self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[BLOCKER])

        result = self.start(loop)

        self.assert_stopped_before_any_session(result, "dependency deadlock")
        for child in (BLOCKER, DEPENDENT):
            self.assertEqual(self.github.labels(child), {"human-review"})
            self.assertIn("dependency deadlock", self.github.comments(child)[-1])


class RecoveryTests(RunLevelTestCase):
    def test_restart_finishes_a_published_child_without_another_session(self):
        for mode in MODES:
            with self.subTest(mode.MODE.diagnostics_label):
                self.new_run()
                self.github.add_issue(CHILD, parent=PARENT)
                self.github.interrupt_after_comment_on = CHILD

                with self.assertRaises(RunInterrupted):
                    self.start(mode, *sessions_per_child(mode))
                self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})
                self.assertIn(mode.MODE.implementation_evidence_prefix, self.github.comments(CHILD)[-1])

                result = self.start(mode)

                self.assert_completed(result, sessions=0)
                self.assertEqual(self.github.labels(CHILD), {"implemented"})
                self.assertFalse(self.github.pull_request().is_draft)

    def test_requeued_child_does_not_reuse_stale_implementation_evidence(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.comment_on_issue(
            CHILD, implementation_evidence(loop.MODE, self.repository.base_sha)["body"]
        )
        self.github.edit_issue_labels(CHILD, add="ready-for-agent", remove=())

        result = self.start(loop, *sessions_per_child(loop))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.session_children(), [CHILD])
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_standard_run_does_not_finish_a_child_published_by_a_reviewed_run(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.comment_on_issue(
            CHILD, implementation_evidence(loop_review.MODE, self.repository.base_sha)["body"]
        )

        result = self.start(loop, *sessions_per_child(loop))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.session_children(), [CHILD])


class ParentCompletionTests(RunLevelTestCase):
    def test_all_children_complete_hands_the_parent_to_human_review_without_sessions(self):
        self.new_run(parent_labels=("ready-for-agent", "implemented", "documentation"))
        self.github.add_issue(CLOSED_BLOCKER, parent=PARENT, state="closed", labels=())
        self.github.add_issue(CHILD, parent=PARENT, labels=("implemented",))

        result = self.start(loop)

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertIn("0 agent sessions and 0 infrastructure retries", result.stdout)
        self.assertIn("implemented children this run: 0", result.stdout)
        self.assertEqual(self.github.labels(PARENT), {"human-review", "documentation"})
        parent_comments = self.github.comments(PARENT)
        self.assertIn("<!-- ralph-experimental-parent-completion -->", parent_comments[-2])
        self.assertIn("all 2 children complete", parent_comments[-2])
        self.assertIn("final outcome: completed.", parent_comments[-1])

    def test_restart_does_not_duplicate_the_parent_comment(self):
        for mode in MODES:
            with self.subTest(mode.MODE.diagnostics_label):
                self.new_run()
                self.github.add_issue(CHILD, parent=PARENT, state="closed", labels=())
                self.github.interrupt_after_comment_on = PARENT

                with self.assertRaises(RunInterrupted):
                    self.start(mode)
                self.assertEqual(self.github.labels(PARENT), {"ready-for-agent"})

                result = self.start(mode)

                self.assert_completed(result, sessions=0)
                marker = mode.MODE.parent_completion_marker
                self.assertEqual(
                    sum(marker in comment for comment in self.github.comments(PARENT)), 1
                )


if __name__ == "__main__":
    unittest.main()
