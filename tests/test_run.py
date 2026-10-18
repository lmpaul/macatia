"""In-memory run tests: the whole loop against a GitHub fake, scripted sessions, and real Git."""

import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import unittest
from unittest import mock


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from ralph_kit import loop, loop_review
from ralph_kit.commands import RalphError
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
    legacy_implementation_evidence,
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
MODES = (loop.MODE, loop_review.MODE)


def passing_child_results(mode):
    """The scripted results of a child that passes on its first try."""
    return [ScriptedResult("committed")] * (2 if mode.review else 1)


class RunLevelTestCase(unittest.TestCase):
    def new_run(self, *, parent_labels=("ready-for-agent",)):
        """A fresh repository and GitHub with only the parent; `setUp` starts one."""
        self.repository = RunRepository(self)
        self.github = FakeGitHub()
        self.github.add_issue(PARENT, labels=parent_labels)

    def setUp(self):
        self.new_run()

    def start(self, mode, *results, budget=None):
        self.sessions = ScriptedSessions(self.repository, *results)
        budget_argument = () if budget is None else (budget,)
        return start_run(mode, self.repository, self.github, self.sessions, PARENT, *budget_argument)

    def session_children(self):
        return [request.child for request in self.sessions.requests]

    def commit_file(self, name, message):
        (self.repository.path / name).write_text(f"{message}\n", encoding="utf-8")
        git(self.repository.path, "add", name)
        git(self.repository.path, "commit", "-m", message)
        return self.repository.head()

    def commit_off_the_branch(self):
        """A real commit that is not an ancestor of the run's branch."""
        git(self.repository.path, "checkout", "-b", "elsewhere", "main")
        head = self.commit_file("elsewhere.txt", "Work on another branch")
        git(self.repository.path, "checkout", BRANCH)
        return head

    def commit_prior_child(self):
        """A real commit on the run's branch, as an earlier run would have published it."""
        return self.commit_file("prior-child.txt", "Implement prior child")

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

        result = self.start(loop_review.MODE, *passing_child_results(loop_review.MODE) * 2)

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

        result = self.start(loop.MODE, *passing_child_results(loop.MODE) * 2)

        self.assert_completed(result, sessions=2)
        self.assertEqual(self.session_children(), [BLOCKER, DEPENDENT])
        self.assertEqual(len(self.repository.commits_since(self.repository.base_sha)), 2)
        self.assertIsNotNone(self.github.pull_request())

    def test_restart_skips_closed_and_implemented_children(self):
        self.github.add_issue(CLOSED_BLOCKER, parent=PARENT, state="closed", labels=())
        self.github.add_issue(BLOCKER, parent=PARENT, labels=("implemented",))
        self.github.add_issue(DEPENDENT, parent=PARENT)

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.session_children(), [DEPENDENT])


class BlockerTests(RunLevelTestCase):
    def add_implemented_blocker_and_dependent(self, evidence):
        self.github.add_issue(BLOCKER, parent=PARENT, labels=("implemented",), comments=[evidence])
        self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[BLOCKER])

    def test_implemented_blocker_satisfies_its_dependent_when_its_verified_head_is_an_ancestor(self):
        for name, evidence in (
            ("standard run evidence", lambda head: implementation_evidence(loop.MODE, head)),
            ("legacy evidence", lambda head: legacy_implementation_evidence(self.repository.base_sha, head)),
        ):
            with self.subTest(name):
                self.new_run()
                verified_head = self.commit_prior_child()
                self.add_implemented_blocker_and_dependent(evidence(verified_head))

                result = self.start(loop.MODE, *passing_child_results(loop.MODE))

                self.assert_completed(result, sessions=1)
                self.assertEqual(self.session_children(), [DEPENDENT])

    def test_blocker_implemented_by_a_reviewed_run_stays_satisfied_in_a_standard_run(self):
        verified_head = self.commit_prior_child()
        self.add_implemented_blocker_and_dependent(
            implementation_evidence(loop_review.MODE, self.repository.base_sha, verified_head)
        )

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.session_children(), [DEPENDENT])

    def test_implemented_blocker_without_ancestor_evidence_stops_before_any_session(self):
        self.add_implemented_blocker_and_dependent(
            implementation_evidence(loop.MODE, self.commit_off_the_branch())
        )

        result = self.start(loop.MODE)

        self.assert_stopped_before_any_session(
            result, f"parent issue #{PARENT} has children remaining but none is runnable"
        )
        self.assertEqual(self.github.labels(DEPENDENT), {"ready-for-agent"})

    def test_dependency_deadlock_stops_without_consuming_agent_sessions(self):
        self.github.add_issue(BLOCKER, parent=PARENT, blocked_by=[DEPENDENT])
        self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[BLOCKER])

        result = self.start(loop.MODE)

        self.assert_stopped_before_any_session(result, "dependency deadlock")
        for child in (BLOCKER, DEPENDENT):
            self.assertEqual(self.github.labels(child), {"human-review"})
            self.assertIn("dependency deadlock", self.github.comments(child)[-1])


class RecoveryTests(RunLevelTestCase):
    def test_restart_finishes_a_published_child_without_another_session(self):
        for mode in MODES:
            with self.subTest(mode.diagnostics_label):
                self.new_run()
                self.github.add_issue(CHILD, parent=PARENT)
                self.github.interrupt_after_comment_on = CHILD

                with self.assertRaises(RunInterrupted):
                    self.start(mode, *passing_child_results(mode))
                self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})
                self.assertIn(mode.implementation_evidence_prefix, self.github.comments(CHILD)[-1])

                result = self.start(mode)

                self.assert_completed(result, sessions=0)
                self.assertEqual(self.github.labels(CHILD), {"implemented"})
                self.assertFalse(self.github.pull_request().is_draft)

    def test_requeued_child_does_not_reuse_stale_implementation_evidence(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.comment_on_issue(
            CHILD, implementation_evidence(loop.MODE, self.repository.base_sha)["body"]
        )
        # Requeuing the child updates it after the evidence, so the evidence is stale.
        self.github.edit_issue_labels(CHILD, add="ready-for-agent", remove=())

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.session_children(), [CHILD])
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_standard_run_does_not_finish_a_child_published_by_a_reviewed_run(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.comment_on_issue(
            CHILD, implementation_evidence(loop_review.MODE, self.repository.base_sha)["body"]
        )

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.session_children(), [CHILD])


class ParentCompletionTests(RunLevelTestCase):
    def test_all_children_complete_hands_the_parent_to_human_review_without_sessions(self):
        self.new_run(parent_labels=("ready-for-agent", "implemented", "documentation"))
        self.github.add_issue(CLOSED_BLOCKER, parent=PARENT, state="closed", labels=())
        self.github.add_issue(CHILD, parent=PARENT, labels=("implemented",))

        result = self.start(loop.MODE)

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
            with self.subTest(mode.diagnostics_label):
                self.new_run()
                self.github.add_issue(CHILD, parent=PARENT, state="closed", labels=())
                self.github.interrupt_after_comment_on = PARENT

                with self.assertRaises(RunInterrupted):
                    self.start(mode)
                self.assertIn(mode.parent_completion_marker, self.github.comments(PARENT)[-1])
                self.assertEqual(self.github.labels(PARENT), {"ready-for-agent"})

                result = self.start(mode)

                self.assert_completed(result, sessions=0)
                marker = mode.parent_completion_marker
                self.assertEqual(
                    sum(marker in comment for comment in self.github.comments(PARENT)), 1
                )


CANONICAL_CHECK = "Canonical CI"
DATABASE_CHECK = "PostgreSQL 16 and Redis acceptance"
PERSISTENCE_PATH = "buyr-hono/prisma/schema.prisma"
FRONTEND_PATH = "buyr-web/src/app/page.tsx"
OTHER_COMMIT = "0" * 40
CI_LOG = "\n".join([
    "AssertionError: expected 42, received 0; token=ci-secret",
    *(f"CI progress {index}" for index in range(100)),
    "last CI log line",
])
# The secrets the scripted local gate prints when it fails; Ralph must never repeat them.
GATE_SECRETS = ("super-secret", "hidden-token", "bearer-secret", "user:password", "```escaped fence")


def evidence_files(prompt, stream):
    return [pathlib.Path(path) for path in re.findall(rf"Full {stream}: (.+)", prompt)]


class VerificationTestCase(RunLevelTestCase):
    """One child under the parent; each test chooses its verification and CI."""

    def new_child_run(self, *, local_gate_results=None, local_checks=True, ci_checks=True, **github):
        self.repository = RunRepository(
            self, local_gate_results=local_gate_results, local_checks=local_checks, ci_checks=ci_checks
        )
        self.github = FakeGitHub(**github)
        self.github.add_issue(PARENT)
        self.github.add_issue(CHILD, parent=PARENT)

    def setUp(self):
        self.new_child_run()

    def roles(self):
        return [request.role for request in self.sessions.requests]

    def assert_implemented(self, result):
        """The child is published as implemented; returns its evidence comment."""
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        self.assertFalse(self.github.pull_request().is_draft)
        return self.github.comments(CHILD)[-1]

    def assert_not_implemented(self, result, reason):
        self.assertEqual(result.exit_code, 1)
        self.assertIn(reason, result.stderr)
        self.assertNotIn("implemented", self.github.labels(CHILD))
        pull = self.github.pull_request()
        self.assertTrue(pull is None or pull.is_draft)


class LocalGateTests(VerificationTestCase):
    def test_review_session_and_ci_only_see_a_head_that_passed_local_gates(self):
        for mode in MODES:
            with self.subTest(mode.diagnostics_label):
                self.new_child_run(local_gate_results=("fail", "pass"))
                review = [ScriptedResult("no_changes")] if mode.review else []

                result = self.start(
                    mode, ScriptedResult("committed"), ScriptedResult("committed"), *review
                )

                self.assert_implemented(result)
                implementation_head, gated_head = self.repository.commits_since(
                    self.repository.base_sha
                )
                self.assertEqual(
                    self.roles(),
                    ["implementation", "local gate correction"] + (["review"] if mode.review else []),
                )
                self.assertEqual(self.repository.local_gate_heads(), [implementation_head, gated_head])
                if mode.review:
                    self.assertIn(f"{self.repository.base_sha}...{gated_head}`", self.sessions.prompts[2])
                self.assertEqual(self.github.checked_heads, [gated_head])

    def test_local_gate_correction_receives_the_complete_redacted_output(self):
        self.new_child_run(local_gate_results=("fail", "pass"))

        result = self.start(loop.MODE, ScriptedResult("committed"), ScriptedResult("committed"))

        self.assert_implemented(result)
        prompt = self.sessions.prompts[1]
        self.assertIn("Repair the failing local gate", prompt)
        self.assertIn(f"Command: {shlex.join(self.repository.local_gate_command)}", prompt)
        self.assertIn("Exit code: 1", prompt)
        self.assertIn("AssertionError: expected local value 42, received 0", prompt)
        self.assertIn("fixture gate failed:", prompt)
        self.assertNotIn("local progress 50", prompt)
        (stdout,), (stderr,) = evidence_files(prompt, "stdout"), evidence_files(prompt, "stderr")
        self.assertIn("local progress 50", stdout.read_text())
        self.assertIn("last local log line", stdout.read_text())
        self.assertIn("[REDACTED]", stderr.read_text())
        diagnostics = "".join(path.read_text() for path in stderr.parent.iterdir())
        for secret in GATE_SECRETS:
            self.assertNotIn(secret, prompt + diagnostics)


class CIEvidenceTests(VerificationTestCase):
    def setUp(self):
        self.new_child_run(ci=("failure", "success"))

    def test_ci_correction_receives_every_failed_check_and_its_verified_job_log(self):
        self.new_child_run(
            ci=("failure", "success"), check_names=(CANONICAL_CHECK, DATABASE_CHECK), job_log_output=CI_LOG
        )

        result = self.start(
            loop.MODE, ScriptedResult("committed", path=PERSISTENCE_PATH), ScriptedResult("committed")
        )

        self.assert_implemented(result)
        commits = self.repository.commits_since(self.repository.base_sha)
        self.assertEqual(self.roles(), ["implementation", "CI correction"])
        prompt = self.sessions.prompts[1]
        self.assertIn("Repair the failing CI check", prompt)
        self.assertIn(f"{CANONICAL_CHECK} concluded failure", prompt)
        self.assertIn(f"{DATABASE_CHECK} concluded failure", prompt)
        self.assertIn("Repository check command: quality-gate", prompt)
        self.assertIn("--job 1001 --log", prompt)
        self.assertIn("--job 1002 --log", prompt)
        self.assertIn("expected 42, received 0", prompt)
        self.assertEqual(self.github.job_log_requests, ["1001", "1002"])
        logs = evidence_files(prompt, "stdout")
        self.assertEqual(len(logs), 2)
        for log in logs:
            self.assertIn("last CI log line", log.read_text())
            self.assertNotIn("ci-secret", log.read_text())
        self.assertNotIn("ci-secret", prompt)
        self.assertEqual(
            [(evidence["head_sha"], evidence["logs_available"]) for evidence in self.repository.ci_evidence()],
            [(commits[0], True)] * 2,
        )
        self.assertEqual(self.github.checked_heads, commits)

    def test_expired_job_logs_leave_explicit_evidence_and_a_job_reference(self):
        self.new_child_run(ci=("failure", "success"), job_log_error="Job logs have expired")

        result = self.start(loop.MODE, ScriptedResult("committed"), ScriptedResult("committed"))

        self.assert_implemented(result)
        prompt = self.sessions.prompts[1]
        self.assertIn("CI logs unavailable. Unable to download job logs.", prompt)
        self.assertIn("Job logs have expired", prompt)
        self.assertIn(f"Job URL: https://github.com/{self.github.repository}/actions/runs/777/job/1001", prompt)
        (evidence,) = self.repository.ci_evidence()
        self.assertFalse(evidence["logs_available"])
        self.assertEqual(evidence["log_error"], "Unable to download job logs.")

    def test_job_from_another_commit_is_never_used_as_evidence(self):
        self.new_child_run(ci=("failure", "success"), job_head_sha=OTHER_COMMIT)

        result = self.start(loop.MODE, ScriptedResult("committed"), ScriptedResult("committed"))

        self.assert_implemented(result)
        prompt = self.sessions.prompts[1]
        self.assertIn("Job metadata does not match the failed commit and check", prompt)
        self.assertNotIn("last CI log line", prompt)
        self.assertEqual(self.github.job_log_requests, [])

    def test_check_without_an_actions_url_keeps_its_annotations(self):
        annotation = "A test assertion failed; logs have no job URL."
        self.new_child_run(
            ci=({CANONICAL_CHECK: {"conclusion": "failure", "details_url": ""}}, "success"),
            check_annotation=annotation,
        )

        result = self.start(loop.MODE, ScriptedResult("committed"), ScriptedResult("committed"))

        self.assert_implemented(result)
        prompt = self.sessions.prompts[1]
        self.assertIn("Job URL: unavailable", prompt)
        self.assertIn("CI logs unavailable. Actions job URL is unavailable", prompt)
        self.assertIn(annotation, prompt)
        self.assertEqual(self.github.job_log_requests, [])

    def test_stale_ci_evidence_never_authorizes_implemented(self):
        self.new_child_run(ci=({CANONICAL_CHECK: {"conclusion": "success", "head_sha": OTHER_COMMIT}},))

        result = self.start(loop.MODE, ScriptedResult("committed"))

        self.assert_not_implemented(result, "infrastructure retries exhausted")
        self.assertIn(
            f"no required current-SHA check evidence is available for: {CANONICAL_CHECK}", result.stderr
        )
        self.assertEqual(self.github.labels(CHILD), {"human-review"})
        self.assertEqual(self.roles(), ["implementation"])

    def test_branch_advancing_after_ci_never_authorizes_implemented(self):
        self.github.ci = ["success"]

        def advance_the_branch(number, body):
            if number == CHILD:
                self.github.after_comment = None
                self.commit_file("advance.txt", "Advance after CI")

        self.github.after_comment = advance_the_branch

        result = self.start(loop.MODE, ScriptedResult("committed"))

        self.assert_not_implemented(result, "branch advanced after canonical CI")

    def test_no_changes_still_requires_ci_before_completion(self):
        result = self.start(loop.MODE, ScriptedResult("no_changes"), ScriptedResult("committed"))

        self.assert_implemented(result)
        self.assertEqual(self.roles(), ["implementation", "CI correction"])
        (correction,) = self.repository.commits_since(self.repository.base_sha)
        self.assertEqual(self.github.checked_heads, [self.repository.base_sha, correction])

    def test_ci_waits_the_default_poll_interval_for_github_actions(self):
        self.new_child_run()

        with mock.patch.dict(os.environ):
            os.environ.pop("RALPH_CI_POLL_SECONDS", None)
            result = self.start(loop.MODE, ScriptedResult("committed"))

        self.assert_implemented(result)
        self.assertEqual(result.sleeps, [45.0])
        self.assertIn("Waiting 45 seconds for GitHub Actions to start.", result.stdout)


class ConditionalCITests(VerificationTestCase):
    def setUp(self):
        self.new_child_run(check_names=(CANONICAL_CHECK, DATABASE_CHECK))

    def test_persistence_change_completes_with_a_green_database_check_for_its_head(self):
        result = self.start(loop.MODE, ScriptedResult("committed", path=PERSISTENCE_PATH))

        self.assert_implemented(result)
        self.assertEqual(self.roles(), ["implementation"])

    def test_persistence_change_corrects_a_failed_or_skipped_database_check(self):
        for conclusion in ("failure", "skipped"):
            with self.subTest(conclusion):
                self.new_child_run(
                    ci=({CANONICAL_CHECK: "success", DATABASE_CHECK: conclusion}, "success"),
                    check_names=(CANONICAL_CHECK, DATABASE_CHECK),
                )

                result = self.start(
                    loop.MODE, ScriptedResult("committed", path=PERSISTENCE_PATH), ScriptedResult("committed")
                )

                self.assert_implemented(result)
                self.assertEqual(self.roles(), ["implementation", "CI correction"])
                self.assertIn(f"{DATABASE_CHECK} concluded {conclusion}", self.sessions.prompts[1])
                self.assertNotIn(f"{CANONICAL_CHECK} concluded", self.sessions.prompts[1])

    def test_persistence_change_without_green_database_evidence_for_its_head_stops(self):
        missing = f"no required current-SHA check evidence is available for: {DATABASE_CHECK}"
        for name, database_check, reason in (
            ("cancelled", {DATABASE_CHECK: "cancelled"}, f"{DATABASE_CHECK} concluded cancelled"),
            (
                "from another commit",
                {DATABASE_CHECK: {"conclusion": "success", "head_sha": OTHER_COMMIT}},
                missing,
            ),
            ("not reported", {}, missing),
        ):
            with self.subTest(name):
                self.new_child_run(ci=({CANONICAL_CHECK: "success", **database_check},))

                result = self.start(loop.MODE, ScriptedResult("committed", path=PERSISTENCE_PATH))

                self.assert_not_implemented(result, "infrastructure retries exhausted")
                self.assertIn(reason, result.stderr)
                self.assertEqual(self.roles(), ["implementation"])

    def test_frontend_only_change_does_not_require_the_database_check(self):
        self.github.ci = [{CANONICAL_CHECK: "success", DATABASE_CHECK: "failure"}]

        result = self.start(loop.MODE, ScriptedResult("committed", path=FRONTEND_PATH))

        self.assert_implemented(result)
        self.assertEqual(self.roles(), ["implementation"])


class VerificationConfigurationTests(VerificationTestCase):
    def test_local_only_run_publishes_with_local_evidence_without_querying_ci(self):
        for mode in MODES:
            with self.subTest(mode.diagnostics_label):
                self.new_child_run(ci_checks=False)

                evidence = self.assert_implemented(self.start(mode, *passing_child_results(mode)))

                head = self.repository.head()
                self.assertEqual(self.github.checked_heads, [])
                self.assertEqual(self.repository.local_gate_heads()[-1], head)
                self.assertEqual(self.repository.pushed_head(), head)
                self.assertIn(f"`{head}`; remote CI was not required.", evidence)
                self.assertNotIn("CI are green", evidence)

    def test_ci_runs_and_corrects_failures_with_local_gates_disabled(self):
        self.new_child_run(local_checks=False, ci=("failure", "success"))

        result = self.start(loop.MODE, ScriptedResult("committed"), ScriptedResult("committed"))

        evidence = self.assert_implemented(result)
        self.assertEqual(self.roles(), ["implementation", "CI correction"])
        self.assertIn("Local quality gates are disabled by Ralph configuration.", result.stdout)
        self.assertEqual(self.repository.local_gate_heads(), [])
        commits = self.repository.commits_since(self.repository.base_sha)
        self.assertEqual(self.github.checked_heads, commits)
        self.assertIn(
            f"Canonical CI is green for exact head `{commits[-1]}`; local gates were explicitly disabled.",
            evidence,
        )

    def test_run_without_automated_verification_warns_and_publishes_unverified_evidence(self):
        for mode in MODES:
            with self.subTest(mode.diagnostics_label):
                # No quality policy is installed, so loading it would stop the run.
                self.new_child_run(local_checks=False, ci_checks=False)

                result = self.start(mode, *passing_child_results(mode))

                evidence = self.assert_implemented(result)
                self.assertIn(mode.unverified_warning, result.stdout)
                self.assertEqual(self.github.checked_heads, [])
                self.assertEqual(self.repository.local_gate_heads(), [])
                self.assertEqual(self.repository.pushed_head(), self.repository.head())
                self.assertIn("automated local gates and remote CI were explicitly disabled", evidence)
                self.assertNotIn("are green", evidence)


class HandoffRedactionTests(VerificationTestCase):
    def test_handoff_comments_keep_the_last_diagnostic_redacted(self):
        corrections = [ScriptedResult("committed")] * 4
        for name, run_options, shown, secrets in (
            ("local gate", {"local_gate_results": ("fail",)}, "fixture gate failed:", GATE_SECRETS),
            ("CI", {"ci": ("failure",), "job_log_output": CI_LOG}, "token=[REDACTED]", ("ci-secret",)),
        ):
            with self.subTest(name):
                self.new_child_run(**run_options)

                result = self.start(loop.MODE, *corrections)

                self.assert_not_implemented(result, "Ralph stopped")
                handoff = self.github.comments(CHILD)[-1]
                self.assertIn("Last diagnostic:", handoff)
                self.assertIn(shown, handoff)
                self.assertIn("[REDACTED]", handoff)
                for secret in secrets:
                    self.assertNotIn(secret, handoff)


class InfrastructureRetryTests(VerificationTestCase):
    def assert_stopped_after_infrastructure_retries(self, result, diagnostic):
        """The run stopped safely after every retry, without another session or publication."""
        self.assert_not_implemented(
            result, f"infrastructure retries exhausted for canonical CI after 3 retries: {diagnostic}"
        )
        self.assertEqual(self.roles(), ["implementation"])
        self.assertEqual(self.github.checked_heads, [self.repository.head()] * 4)
        self.assertEqual(self.github.labels(CHILD), {"human-review"})
        self.assertIn(
            "without changing the implementation or consuming another agent session",
            self.github.comments(CHILD)[-1],
        )
        self.assertIn(f"Last infrastructure diagnostic: {diagnostic}", self.github.comments(CHILD)[-1])
        self.assertIn("final outcome: stopped safely.", self.github.comments(PARENT)[-1])
        self.assertIn("final outcome: stopped safely.", self.github.pull_request().comments[-1])

    def test_unavailable_cancelled_and_timed_out_ci_is_retried_without_another_session(self):
        for conclusion in ("unavailable", "cancelled", "timed_out"):
            with self.subTest(conclusion):
                self.new_child_run(ci=(conclusion,))

                with mock.patch.dict(os.environ, {"RALPH_INFRA_RETRY_SECONDS": "30"}):
                    result = self.start(loop.MODE, ScriptedResult("committed"))

                self.assert_stopped_after_infrastructure_retries(
                    result, f"{CANONICAL_CHECK} concluded {conclusion}"
                )
                self.assertEqual(result.sleeps[-3:], [30.0] * 3)
                self.assertIn("Checking canonical CI (attempt 4/4).", result.stdout)

    def test_authentication_failures_and_service_outages_are_infrastructure_failures(self):
        for failure in ("HTTP 401: Bad credentials", "HTTP 503: Service Unavailable"):
            with self.subTest(failure):
                self.new_child_run(ci=(RalphError(f"gh api failed: {failure}"),))

                result = self.start(loop.MODE, ScriptedResult("committed"))

                self.assert_stopped_after_infrastructure_retries(result, f"gh api failed: {failure}")

    def test_infrastructure_retry_that_succeeds_does_not_consume_the_run_budget(self):
        for mode in MODES:
            with self.subTest(mode.diagnostics_label):
                self.new_child_run(ci=("cancelled", "success"))
                sessions = passing_child_results(mode)

                result = self.start(mode, *sessions, budget=len(sessions))

                self.assert_implemented(result)
                self.assertIn(
                    f"{len(sessions)} agent sessions and 1 infrastructure retries this run.", result.stdout
                )
                self.assertIn("Canonical CI is not ready; the next check is in", result.stdout)
                self.assertIn("Checking canonical CI (attempt 2/4).", result.stdout)
                self.assertEqual(self.github.checked_heads, [self.repository.head()] * 2)


class RunBudgetTests(VerificationTestCase):
    def new_two_child_run(self):
        self.new_run()
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.add_issue(DEPENDENT, parent=PARENT)

    def assert_budget_stop(self, result, child, *, role, budget):
        """The run stopped on the shared budget and handed `child` to human review."""
        self.assertEqual(result.exit_code, 1)
        self.assertIn(f"run budget exhausted before {role}", result.stderr)
        self.assertEqual(len(self.sessions.requests), budget)
        self.assertEqual(self.github.labels(child), {"human-review"})
        handoff = self.github.comments(child)[-1]
        self.assertIn(
            f"run budget of {budget} agent sessions was exhausted after {budget} sessions", handoff
        )
        self.assertIn("final outcome: stopped safely.", self.github.comments(PARENT)[-1])
        self.assertEqual(self.github.labels(PARENT), {"ready-for-agent"})
        return handoff

    def test_default_budget_allows_five_sessions_per_child_across_the_run(self):
        self.new_two_child_run()
        committed = ScriptedResult("committed")
        findings = ScriptedResult("needs_correction", findings=("Expose freshness for each section.",))
        # Implementation, then two reviews with findings, each followed by an issue correction.
        reviewed_twice = [committed, findings, committed, findings, committed]

        result = self.start(
            loop_review.MODE, *reviewed_twice, ScriptedResult("no_changes"), *reviewed_twice[:4]
        )

        self.assert_budget_stop(result, DEPENDENT, role="issue correction", budget=10)
        self.assertEqual(self.session_children(), [CHILD] * 6 + [DEPENDENT] * 4)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        self.assertTrue(self.github.pull_request().is_draft)

    def test_explicit_budget_replaces_the_default_across_children(self):
        self.new_two_child_run()
        results = [*passing_child_results(loop_review.MODE), ScriptedResult("committed")]

        result = self.start(loop_review.MODE, *results, budget=3)

        self.assert_budget_stop(result, DEPENDENT, role="review", budget=3)
        self.assertEqual(self.session_children(), [CHILD, CHILD, DEPENDENT])
        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        self.assertTrue(self.github.pull_request().is_draft)

    def test_budget_stops_keep_the_last_evidence_for_the_handoff_and_do_not_publish(self):
        local_gate = ("local gate correction", "fixture gate failed:", False)
        for name, run_options, (role, evidence, pushed) in (
            ("local gate", {"local_gate_results": ("fail",)}, local_gate),
            ("local gate with CI disabled", {"local_gate_results": ("fail",), "ci_checks": False}, local_gate),
            (
                "CI",
                {"ci": ("failure",), "job_log_output": CI_LOG},
                ("CI correction", "expected 42, received 0", True),
            ),
        ):
            with self.subTest(name):
                self.new_child_run(**run_options)

                result = self.start(loop.MODE, ScriptedResult("committed"), budget=1)

                handoff = self.assert_budget_stop(result, CHILD, role=role, budget=1)
                self.assertIn(evidence, handoff)
                self.assertIn("Full stdout:", handoff)
                pull = self.github.pull_request()
                if pushed:
                    self.assertTrue(pull.is_draft)
                else:
                    self.assertIsNone(pull)
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.repository.pushed_head()


if __name__ == "__main__":
    unittest.main()
