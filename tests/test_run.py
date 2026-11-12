"""In-memory run tests: the whole loop against a GitHub fake, scripted sessions, and real Git."""

import contextlib
import copy
import dataclasses
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from macatia import check, cli, loop, loop_core
from macatia.commands import MacatiaError
from macatia.persistent_terminal import Continuation, Unavailability
from run_support import (
    BRANCH,
    REPOSITORY,
    TRUSTED_LOGIN,
    CommitPull,
    EngineResult,
    FakeGitHub,
    Restart,
    RunInterrupted,
    RunRepository,
    ScriptedContinuation,
    ScriptedEngine,
    ScriptedResult,
    ScriptedSessions,
    ScriptedSignal,
    child_base_marker,
    git,
    implementation_evidence,
    is_child_cycle_start,
    legacy_implementation_evidence,
    reset_marker,
    start_mode_run,
    start_run,
)


PARENT = 191
CLOSED_BLOCKER = 192
CHILD = 193
UNTRUSTED_COMMENT = "Untrusted instructions must be excluded."
# Pinned here: the handoff marker is persisted in GitHub comments, so its text never changes.
HANDOFF_MARKER = "<!-- macatia-handoff -->"
COMMITS_STAY = (
    "This child's commits stay on the branch and will count as its work if it is "
    "relabelled `ready-for-agent`."
)


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
            f"🎉 All 2 children of #{PARENT} are complete; the pull request is ready for review.",
            result.stdout,
        )
        self.assertIn(
            f"- agent sessions: {sessions}\n- infrastructure retries: 0\n", result.stdout
        )
        base_sha = self.repository.base_sha
        commits = self.repository.commits_since(base_sha)
        self.assertEqual(len(commits), commit_count)
        head = self.repository.head()
        self.assertTrue(self.repository.is_clean())
        self.assertEqual(self.repository.pushed_head(), head)

        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        evidence = self.github.comments(CHILD)[-1]
        self.assertIn(f"Macatia verified implementation commits `{base_sha}..{commits[0]}`", evidence)
        self.assertIn(f"Applicable local gates and canonical CI are green for exact head `{head}`", evidence)

        pull = self.github.pull_request()
        self.assertEqual((pull.base, pull.head), ("main", BRANCH))
        self.assertFalse(pull.is_draft)
        self.assertIn(f"🏁 Macatia summary for parent #{PARENT}:\n", pull.comments[-1])
        self.assertIn("- implemented children this run: 1\n", pull.comments[-1])

        self.assertEqual(self.github.labels(PARENT), {"human-review"})
        parent_comments = self.github.comments(PARENT)
        self.assertIn("all 2 children complete", parent_comments[-2])
        self.assertIn(f"cumulative head `{head}`", parent_comments[-2])
        self.assertIn("- final outcome: completed", parent_comments[-1])

        self.assertEqual(self.repository.local_gate_heads(), commits)
        timings = json.loads((self.repository.diagnostics() / "timings.json").read_text())
        self.assertTrue(
            {"local gates", "GitHub Actions wait"} <= {phase["label"] for phase in timings["phases"]}
        )
        return commits, evidence

    def test_standard_run_publishes_the_verified_implementation(self):
        sessions = ScriptedSessions(self.repository, ScriptedResult("committed"))

        result = start_mode_run(loop_core.STANDARD_RUN, self.repository, self.github, sessions, PARENT)

        commits, evidence = self.assert_child_published(result, sessions=1, commit_count=1)
        self.assert_implementation_prompt_names_only_the_issue(sessions)
        self.assertIn("macatia-experimental-implementation-evidence", evidence)
        self.assertIn("No separate Macatia review session was run.", evidence)
        self.assertIn(f"verified head `{commits[0]}` with no corrections", evidence)
        self.assertIn("<!-- macatia-experimental-parent-completion -->", self.github.comments(PARENT)[-2])

    def test_reviewed_run_publishes_the_review_session_fix(self):
        sessions = ScriptedSessions(
            self.repository,
            ScriptedResult("committed"),
            ScriptedResult("committed", "Reviewed the exact diff and committed one correction."),
        )

        # `--review` is accepted in any position, here after the parent.
        result = start_run(self.repository, self.github, sessions, PARENT, "--review")

        commits, evidence = self.assert_child_published(result, sessions=2, commit_count=2)
        self.assert_implementation_prompt_names_only_the_issue(sessions)
        self.assertIn(f"Review the exact `{self.repository.base_sha}...{commits[0]}` diff", sessions.prompts[1])
        self.assertIn("<!-- macatia-implementation-evidence: ", evidence)
        self.assertIn(f"corrective review commits `{commits[0]}..{commits[1]}`", evidence)
        self.assertIn("<!-- macatia-parent-completion -->", self.github.comments(PARENT)[-2])


BLOCKER = 193
DEPENDENT = 194
MODES = (loop_core.STANDARD_RUN, loop_core.REVIEWED_RUN)


def passing_child_results(mode):
    """The scripted results of a child that passes on its first try."""
    return [ScriptedResult("committed")] * (2 if mode.review else 1)


def worst_case_child_results(mode):
    """The scripted results of a child that passes after every correction the limit allows.

    Its last review, in a reviewed run, returns no_changes.
    """
    committed = ScriptedResult("committed")
    if not mode.review:
        corrections = ScriptedResult("needs_correction", findings=("Handle empty carts.",), commit=True)
        return [corrections] * 3 + [committed]
    findings = ScriptedResult("needs_correction", findings=("Handle empty carts.",))
    return [committed, findings] * 3 + [committed, ScriptedResult("no_changes")]


def set_child_session_limit(repository, **limits):
    """Configure `[child_session_limit]` in `repository`'s macatia.toml with `limits`."""
    table = "".join(f"{key} = {value}\n" for key, value in limits.items())
    repository.edit_configuration(
        "[verification]\n", f"[child_session_limit]\n{table}\n[verification]\n"
    )


class RunLevelTestCase(unittest.TestCase):
    # Called with each session request before it is answered; see `ready_for_agent_during_the_run`.
    before_session = None

    def new_run(self, *, parent_labels=("ready-for-agent",)):
        """A fresh repository and GitHub with only the parent; `setUp` starts one."""
        self.repository = RunRepository(self)
        self.github = FakeGitHub()
        self.github.add_issue(PARENT, labels=parent_labels)

    def setUp(self):
        self.new_run()

    def start(self, mode, *results, confirm=None, terminal=False):
        self.sessions = ScriptedSessions(self.repository, *results)
        self.sessions.before_session = self.before_session
        return start_mode_run(
            mode, self.repository, self.github, self.sessions, PARENT,
            confirm=confirm, terminal=terminal,
        )

    def session_children(self):
        return [request.child for request in self.sessions.requests]

    def ready_for_agent_during_the_run(self, child):
        """`child` is ready for a human when the run starts and ready for an agent from its first session."""
        self.github.add_issue(child, parent=PARENT, labels=("ready-for-human",))

        def relabel(request):
            if self.github.labels(child) == {"ready-for-human"}:
                self.github.edit_issue_labels(child, add="ready-for-agent", remove=["ready-for-human"])

        self.before_session = relabel

    def commit_file(self, name, message):
        (self.repository.path / name).write_text(f"{message}\n", encoding="utf-8")
        git(self.repository.path, "add", name)
        git(self.repository.path, "commit", "-m", message)
        return self.repository.head()

    def commit_off_the_branch(self, branch="elsewhere"):
        """A real commit that is not an ancestor of the run's branch, made on `branch`."""
        git(self.repository.path, "checkout", "-b", branch, "main")
        head = self.commit_file(f"{branch}.txt", "Work on another branch")
        git(self.repository.path, "checkout", BRANCH)
        return head

    def commit_prior_child(self):
        """A real commit on the run's branch, as an earlier run would have published it."""
        return self.commit_file("prior-child.txt", "Implement prior child")

    def assert_completed(self, result, *, sessions):
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertIn(
            f"- agent sessions: {sessions}\n- infrastructure retries: 0\n", result.stdout
        )
        self.assertEqual(self.github.labels(PARENT), {"human-review"})

    def assert_stopped_before_any_session(self, result, reason):
        self.assertEqual(result.exit_code, 1)
        self.assertIn(reason, result.stderr)
        self.assertEqual(self.sessions.requests, [])
        self.assertIsNone(self.github.pull_request())
        self.assertIn("- final outcome: stopped safely", self.github.comments(PARENT)[-1])


class ChildSelectionTests(RunLevelTestCase):
    def test_dependent_children_run_in_dependency_order_on_one_branch_and_pr(self):
        self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[BLOCKER])
        self.github.add_issue(BLOCKER, parent=PARENT, blocked_by=[CLOSED_BLOCKER])
        self.github.add_issue(CLOSED_BLOCKER, parent=PARENT, state="closed")

        result = self.start(loop_core.REVIEWED_RUN, *passing_child_results(loop_core.REVIEWED_RUN) * 2)

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

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN) * 2)

        self.assert_completed(result, sessions=2)
        self.assertEqual(self.session_children(), [BLOCKER, DEPENDENT])
        self.assertEqual(len(self.repository.commits_since(self.repository.base_sha)), 2)
        self.assertIsNotNone(self.github.pull_request())

    def test_restart_skips_closed_and_implemented_children(self):
        self.github.add_issue(CLOSED_BLOCKER, parent=PARENT, state="closed", labels=())
        self.github.add_issue(BLOCKER, parent=PARENT, labels=("implemented",))
        self.github.add_issue(DEPENDENT, parent=PARENT)

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.session_children(), [DEPENDENT])


class BlockerTests(RunLevelTestCase):
    def add_implemented_blocker_and_dependent(self, evidence):
        self.github.add_issue(BLOCKER, parent=PARENT, labels=("implemented",), comments=[evidence])
        self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[BLOCKER])

    def test_implemented_blocker_satisfies_its_dependent_when_its_verified_head_is_an_ancestor(self):
        for name, evidence in (
            ("standard run evidence", lambda head: implementation_evidence(loop_core.STANDARD_RUN, head)),
            ("legacy evidence", lambda head: legacy_implementation_evidence(self.repository.base_sha, head)),
        ):
            with self.subTest(name):
                self.new_run()
                verified_head = self.commit_prior_child()
                self.add_implemented_blocker_and_dependent(evidence(verified_head))

                result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

                self.assert_completed(result, sessions=1)
                self.assertEqual(self.session_children(), [DEPENDENT])
                self.assertEqual(self.github.pull_lookups, [])

    def test_implemented_blocker_satisfies_its_dependent_when_its_merged_pull_request_is_on_the_branch(self):
        verified_head = self.commit_off_the_branch()
        self.github.commit_pulls[verified_head] = [CommitPull(43, self.commit_prior_child())]
        self.add_implemented_blocker_and_dependent(
            implementation_evidence(loop_core.STANDARD_RUN, verified_head)
        )

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.session_children(), [DEPENDENT])

    def test_implemented_blocker_whose_merged_pull_request_is_not_on_the_branch_stops_before_any_session(self):
        verified_head = self.commit_off_the_branch()
        on_the_branch = self.commit_prior_child()
        # Only a pull request merged into the base branch counts, whatever its merge commit.
        self.github.commit_pulls[verified_head] = [
            CommitPull(41, on_the_branch, state="closed"),
            CommitPull(42, on_the_branch, state="open"),
            CommitPull(43, on_the_branch, base="preprod"),
            CommitPull(44, self.commit_off_the_branch("merged-elsewhere")),
        ]
        self.add_implemented_blocker_and_dependent(
            implementation_evidence(loop_core.STANDARD_RUN, verified_head)
        )

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_stopped_before_any_session(
            result, f"parent issue #{PARENT} has children remaining but none is runnable"
        )
        self.assertEqual(self.github.labels(DEPENDENT), {"ready-for-agent"})
        self.assertEqual(self.github.pull_lookups, [("main", verified_head)])

    def test_github_error_during_the_pull_request_lookup_of_a_blocker_stops_after_the_infrastructure_retries(self):
        verified_head = self.commit_off_the_branch()
        self.github.commit_pulls[verified_head] = MacatiaError("gh api failed: HTTP 503: Service Unavailable")
        self.add_implemented_blocker_and_dependent(
            implementation_evidence(loop_core.STANDARD_RUN, verified_head)
        )

        with mock.patch.dict(os.environ, {"MACATIA_INFRA_RETRY_SECONDS": "30"}):
            result = self.start(loop_core.STANDARD_RUN)

        self.assert_stopped_before_any_session(
            result,
            f"infrastructure retries exhausted for the merged pull request lookup of blocker #{BLOCKER} "
            "after 3 retries: gh api failed: HTTP 503: Service Unavailable",
        )
        self.assertNotIn("dependency deadlock", result.stderr)
        self.assertEqual(self.github.pull_lookups, [("main", verified_head)] * 4)
        self.assertEqual(result.sleeps[-3:], [30.0] * 3)
        self.assertIn("- infrastructure retries: 3\n", self.github.comments(PARENT)[-1])
        self.assertIn(f"- #{DEPENDENT}: waiting on #{BLOCKER}", result.stderr)
        self.assertEqual(self.github.labels(DEPENDENT), {"ready-for-agent"})

    def test_closed_blocker_satisfies_its_dependent_whatever_its_evidence(self):
        verified_head = self.commit_off_the_branch()
        self.github.add_issue(
            CLOSED_BLOCKER,
            parent=PARENT,
            state="closed",
            comments=[implementation_evidence(loop_core.STANDARD_RUN, verified_head)],
        )
        self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[CLOSED_BLOCKER])

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.session_children(), [DEPENDENT])
        self.assertEqual(self.github.pull_lookups, [])

    def test_blocker_implemented_by_a_reviewed_run_stays_satisfied_in_a_standard_run(self):
        verified_head = self.commit_prior_child()
        self.add_implemented_blocker_and_dependent(
            implementation_evidence(loop_core.REVIEWED_RUN, self.repository.base_sha, verified_head)
        )

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.session_children(), [DEPENDENT])

    def test_evidence_older_than_the_blockers_reset_marker_does_not_satisfy_its_dependent(self):
        verified_head = self.commit_prior_child()
        self.github.add_issue(BLOCKER, parent=PARENT, labels=("implemented",), comments=[
            implementation_evidence(loop_core.STANDARD_RUN, verified_head),
            reset_marker(),
        ])
        self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[BLOCKER])

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_stopped_before_any_session(
            result, f"parent issue #{PARENT} has children remaining but none is runnable"
        )
        self.assertEqual(self.github.labels(DEPENDENT), {"ready-for-agent"})

    def test_evidence_after_the_blockers_reset_marker_or_before_an_untrusted_one_satisfies_its_dependent(self):
        for name, comments in (
            ("evidence after the reset marker", lambda head: [
                reset_marker(), implementation_evidence(loop_core.STANDARD_RUN, head),
            ]),
            ("untrusted reset marker", lambda head: [
                implementation_evidence(loop_core.STANDARD_RUN, head),
                reset_marker(author="outside-contributor"),
            ]),
        ):
            with self.subTest(name):
                self.new_run()
                verified_head = self.commit_prior_child()
                self.github.add_issue(
                    BLOCKER, parent=PARENT, labels=("implemented",), comments=comments(verified_head)
                )
                self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[BLOCKER])

                result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

                self.assert_completed(result, sessions=1)
                self.assertEqual(self.session_children(), [DEPENDENT])

    def test_implemented_blocker_without_ancestor_evidence_stops_before_any_session(self):
        self.add_implemented_blocker_and_dependent(
            implementation_evidence(loop_core.STANDARD_RUN, self.commit_off_the_branch())
        )

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_stopped_before_any_session(
            result, f"parent issue #{PARENT} has children remaining but none is runnable"
        )
        self.assertEqual(self.github.labels(DEPENDENT), {"ready-for-agent"})

    def test_dependency_deadlock_stops_without_consuming_agent_sessions(self):
        self.github.add_issue(BLOCKER, parent=PARENT, blocked_by=[DEPENDENT])
        self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[BLOCKER])

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_stopped_before_any_session(result, "dependency deadlock")
        for child in (BLOCKER, DEPENDENT):
            self.assertEqual(self.github.labels(child), {"human-review"})
            self.assertIn("dependency deadlock", self.github.comments(child)[-1])
            self.assertNotIn(COMMITS_STAY, self.github.comments(child)[-1])


class RecoveryTests(RunLevelTestCase):
    def test_restart_finishes_a_published_child_without_another_session(self):
        for mode in MODES:
            with self.subTest(mode.diagnostics_label):
                self.new_run()
                self.github.add_issue(CHILD, parent=PARENT)
                self.github.interrupt_after_comment_on = CHILD
                self.github.interrupt_after_comment_containing = mode.implementation_evidence_prefix

                with self.assertRaises(RunInterrupted):
                    self.start(mode, *passing_child_results(mode))
                self.assertEqual(self.github.labels(CHILD), {"agent-working"})
                self.assertIn(mode.implementation_evidence_prefix, self.github.comments(CHILD)[-1])

                result = self.start(mode)

                self.assert_completed(result, sessions=0)
                self.assertEqual(self.github.labels(CHILD), {"implemented"})
                self.assertFalse(self.github.pull_request().is_draft)

    def test_requeued_child_does_not_reuse_stale_implementation_evidence(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.comment_on_issue(
            CHILD, implementation_evidence(loop_core.STANDARD_RUN, self.repository.base_sha)["body"]
        )
        # Requeuing the child updates it after the evidence, so the evidence is stale.
        self.github.edit_issue_labels(CHILD, add="ready-for-agent", remove=())

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.session_children(), [CHILD])
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_standard_run_does_not_finish_a_child_published_by_a_reviewed_run(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.comment_on_issue(
            CHILD, implementation_evidence(loop_core.REVIEWED_RUN, self.repository.base_sha)["body"]
        )

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.session_children(), [CHILD])

    def add_published_child(self, *later_comments):
        """A ready child whose evidence, on the branch, is followed by `later_comments`
        posted in the same second, so the evidence's time is still the child's latest update."""
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.comment_on_issue(
            CHILD, implementation_evidence(loop_core.STANDARD_RUN, self.repository.base_sha)["body"]
        )
        updated_at = self.github.issues[CHILD]["updatedAt"]
        for comment in later_comments:
            self.github.issues[CHILD]["comments"].append({**comment, "createdAt": updated_at})

    def test_child_whose_reset_marker_is_newer_than_its_evidence_is_implemented_again(self):
        self.add_published_child(reset_marker())

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.session_children(), [CHILD])
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_untrusted_reset_marker_does_not_stop_the_recovery_of_a_published_child(self):
        self.add_published_child(reset_marker(author="outside-contributor"))

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_completed(result, sessions=0)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})


class AgentWorkingTests(RunLevelTestCase):
    """The child a run works on is labelled agent-working until it is implemented or handed off."""

    def start_observing_labels(self, mode, *results):
        """Start a run that records the child's labels as each agent session starts."""
        self.sessions = ScriptedSessions(self.repository, *results)
        observed = []
        self.sessions.before_session = lambda request: observed.append(
            (request.role, self.github.labels(request.child))
        )
        result = start_mode_run(mode, self.repository, self.github, self.sessions, PARENT)
        return result, observed

    def test_child_is_agent_working_during_every_session_of_its_child_cycle(self):
        self.github.add_issue(CHILD, parent=PARENT)
        findings = ScriptedResult("needs_correction", findings=("Handle empty carts.",))

        result, observed = self.start_observing_labels(
            loop_core.REVIEWED_RUN,
            ScriptedResult("committed"),
            findings,
            ScriptedResult("committed"),
            ScriptedResult("no_changes"),
        )

        self.assert_completed(result, sessions=4)
        self.assertEqual(
            observed,
            [
                ("implementation", {"agent-working"}),
                ("review", {"agent-working"}),
                ("issue correction", {"agent-working"}),
                ("review", {"agent-working"}),
            ],
        )
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_implemented_child_carries_only_the_implemented_lifecycle_label(self):
        self.github.add_issue(CHILD, parent=PARENT, labels=("ready-for-agent", "documentation"))

        def human_adds_lifecycle_labels(number, body):
            if number == CHILD:
                self.github.edit_issue_labels(CHILD, add="human-review", remove=())
                self.github.edit_issue_labels(CHILD, add="ready-for-agent", remove=())

        self.github.after_comment = human_adds_lifecycle_labels

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.github.labels(CHILD), {"implemented", "documentation"})

    def test_child_handed_to_human_review_carries_only_the_human_review_lifecycle_label(self):
        self.github.add_issue(CHILD, parent=PARENT)

        def human_adds_lifecycle_label(number, body):
            if number == CHILD and not is_child_cycle_start(body):
                self.github.edit_issue_labels(CHILD, add="ready-for-agent", remove=())

        self.github.after_comment = human_adds_lifecycle_label

        result, observed = self.start_observing_labels(
            loop_core.STANDARD_RUN, ScriptedResult("blocked", "Needs a product decision.")
        )

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(observed, [("implementation", {"agent-working"})])
        self.assertEqual(self.github.labels(CHILD), {"human-review"})

    def test_failed_agent_working_label_write_stops_before_any_session(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.label_edit_error = MacatiaError("gh issue edit failed: HTTP 502")

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_stopped_before_any_session(result, "gh issue edit failed: HTTP 502")
        self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})


class HandoffMarkerTests(RunLevelTestCase):
    """Every comment that sends a child to human review carries the handoff marker."""

    def assert_handoffs_alone_carry_the_marker(self, handoffs):
        """Each child in `handoffs` was last sent to human review with the given visible text,
        and no other comment of the run carries the handoff marker."""
        for child, visible_text in handoffs.items():
            self.assertEqual(self.github.labels(child), {"human-review"})
            self.assertEqual(self.github.comments(child)[-1], f"{HANDOFF_MARKER}\n{visible_text}")
        for number in self.github.issues:
            comments = self.github.comments(number)
            if number in handoffs:
                comments = comments[:-1]
            for body in comments:
                self.assertNotIn(HANDOFF_MARKER, body)

    def test_handoff_of_a_started_child_carries_the_marker_in_both_run_modes(self):
        for mode in MODES:
            with self.subTest(mode.diagnostics_label):
                self.new_run()
                self.github.add_issue(CHILD, parent=PARENT)

                result = self.start(mode, ScriptedResult("blocked", "Needs a product decision."))

                self.assertEqual(result.exit_code, 1, result.stderr)
                self.assert_handoffs_alone_carry_the_marker({
                    CHILD: "Macatia stopped because implementation reported blocked: Needs a product "
                    f"decision. This child remains open for human review.\n\n{COMMITS_STAY}",
                })

    def test_handoff_without_commits_carries_the_marker_in_both_run_modes(self):
        deadlock = (
            "Macatia stopped because the parent has a dependency deadlock. This child remains "
            "open for human review."
        )
        for mode in MODES:
            with self.subTest(mode.diagnostics_label):
                self.new_run()
                self.github.add_issue(BLOCKER, parent=PARENT, blocked_by=[DEPENDENT])
                self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[BLOCKER])

                result = self.start(mode)

                self.assert_stopped_before_any_session(result, "dependency deadlock")
                self.assert_handoffs_alone_carry_the_marker({BLOCKER: deadlock, DEPENDENT: deadlock})

    def test_comments_of_a_completed_run_carry_no_handoff_marker(self):
        for mode in MODES:
            with self.subTest(mode.diagnostics_label):
                self.new_run()
                self.github.add_issue(CHILD, parent=PARENT)

                result = self.start(mode, *passing_child_results(mode))

                self.assert_completed(result, sessions=len(passing_child_results(mode)))
                self.assert_handoffs_alone_carry_the_marker({})


class ScriptedConfirm:
    """Answers the run's startup questions as the person at the terminal would.

    `answer` is True (the accepting choice, such as Resume), False (Stop), or an
    exception the prompt raises. `choices` keeps, for each question, the label
    of its accepting choice and whether that choice is the default.
    """

    def __init__(self, answer):
        self.answer = answer
        self.questions = []
        self.choices = []

    def __call__(self, question, accept, *, accept_by_default):
        self.questions.append(question)
        self.choices.append((accept, accept_by_default))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


STOP, RESUME, CREATE, SWITCH = False, True, True, True


class LeftAgentWorkingTests(RunLevelTestCase):
    """A run that starts with children left agent working asks before resuming them."""

    def assert_warning(self, text, *children):
        names = ", ".join(f"#{child}" for child in children)
        self.assertIn(f"{names} labelled agent-working", text)
        self.assertIn("Macatia cannot tell an interrupted run from a run still active elsewhere", text)
        self.assertIn("two agents do the same work on two branches", text)
        self.assertNotIn("not covered by their implementation evidence", text)

    def assert_stopped_on_agent_working(self, result, *children):
        self.assert_stopped_before_any_session(result, "labelled agent-working")
        summary = self.github.comments(PARENT)[-1]
        self.assertIn(f"- remaining children: {len(self.github.sub_issues(PARENT))}\n", summary)
        for child in children:
            self.assertEqual(self.github.labels(child), {"agent-working"})
            self.assertIn(
                f"- #{child}: agent working from an earlier or concurrent run.", summary
            )
            self.assertIn(
                f"- If no other run is working on this parent, inspect the branch, relabel "
                f"#{child} ready-for-agent, then run `macatia run {PARENT}` again.",
                summary,
            )
        self.assertNotIn("Fix the stop reason", summary)
        return summary

    def test_stop_leaves_the_child_agent_working_and_starts_no_session(self):
        self.github.add_issue(CHILD, parent=PARENT, labels=("agent-working",))
        self.github.add_issue(DEPENDENT, parent=PARENT)
        confirm = ScriptedConfirm(STOP)

        result = self.start(loop_core.STANDARD_RUN, confirm=confirm)

        self.assert_stopped_on_agent_working(result, CHILD)
        self.assertEqual(self.github.labels(DEPENDENT), {"ready-for-agent"})
        self.assertEqual(confirm.choices, [("Resume", False)])
        (question,) = confirm.questions
        self.assert_warning(result.stdout + question, CHILD)
        self.assertIn(
            f"If no other run is working on this parent, inspect the branch, relabel #{CHILD} "
            "ready-for-agent",
            result.stderr,
        )

    def test_resume_relabels_the_child_and_implements_it_in_the_same_run(self):
        self.github.add_issue(CHILD, parent=PARENT, labels=("agent-working",))
        confirm = ScriptedConfirm(RESUME)

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN), confirm=confirm)

        self.assert_completed(result, sessions=1)
        self.assertEqual(len(confirm.questions), 1)
        self.assertEqual(self.session_children(), [CHILD])
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_resumed_child_gets_a_fresh_child_session_limit(self):
        self.github.add_issue(CHILD, parent=PARENT, labels=("agent-working",))
        self.github.add_issue(DEPENDENT, parent=PARENT)
        findings = ScriptedResult("needs_correction", findings=("Handle empty carts.",), commit=True)

        set_child_session_limit(self.repository, standard=2)

        result = self.start(
            loop_core.STANDARD_RUN, findings, ScriptedResult("committed"),
            findings, ScriptedResult("committed"), confirm=ScriptedConfirm(RESUME),
        )

        self.assert_completed(result, sessions=4)
        self.assertEqual(self.session_children(), [CHILD] * 2 + [DEPENDENT] * 2)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_several_children_get_one_warning_and_one_choice(self):
        self.github.add_issue(CHILD, parent=PARENT, labels=("agent-working",))
        self.github.add_issue(DEPENDENT, parent=PARENT, labels=("agent-working",))
        confirm = ScriptedConfirm(RESUME)

        result = self.start(
            loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN) * 2, confirm=confirm
        )

        self.assert_completed(result, sessions=2)
        (question,) = confirm.questions
        self.assertEqual((result.stdout + question).count("labelled agent-working"), 1)
        self.assert_warning(result.stdout + question, CHILD, DEPENDENT)
        self.assertEqual(self.session_children(), [CHILD, DEPENDENT])

    def test_several_children_stopped_are_each_listed(self):
        self.github.add_issue(CHILD, parent=PARENT, labels=("agent-working",))
        self.github.add_issue(DEPENDENT, parent=PARENT, labels=("agent-working",))

        result = self.start(loop_core.STANDARD_RUN, confirm=ScriptedConfirm(STOP))

        self.assert_stopped_on_agent_working(result, CHILD, DEPENDENT)

    def test_without_a_terminal_the_run_stops_with_the_warning_in_its_summary(self):
        self.github.add_issue(CHILD, parent=PARENT, labels=("agent-working",))

        result = self.start(loop_core.STANDARD_RUN, confirm=None)

        summary = self.assert_stopped_on_agent_working(result, CHILD)
        self.assert_warning(summary, CHILD)

    def test_ctrl_c_at_the_prompt_counts_as_stop(self):
        self.github.add_issue(CHILD, parent=PARENT, labels=("agent-working",))

        result = self.start(loop_core.STANDARD_RUN, confirm=ScriptedConfirm(KeyboardInterrupt()))

        self.assert_stopped_on_agent_working(result, CHILD)

    def test_published_agent_working_child_is_recovered_without_a_prompt(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.interrupt_after_comment_on = CHILD
        self.github.interrupt_after_comment_containing = loop_core.STANDARD_RUN.implementation_evidence_prefix
        with self.assertRaises(RunInterrupted):
            self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))
        self.assertEqual(self.github.labels(CHILD), {"agent-working"})
        confirm = ScriptedConfirm(STOP)

        result = self.start(loop_core.STANDARD_RUN, confirm=confirm)

        self.assert_completed(result, sessions=0)
        self.assertEqual(confirm.questions, [])
        self.assertEqual(self.github.labels(CHILD), {"implemented"})


LATER_CHILD = 199


class ConcurrentRunTests(RunLevelTestCase):
    """A run that finds another run's child agent working at its next child selection stops."""

    def start_with_another_run(self, mode, *results, published=False):
        """Start a run during whose first session another run takes DEPENDENT.

        With `published`, the other run has also published DEPENDENT's
        implementation evidence but not yet marked it implemented.
        """
        self.sessions = ScriptedSessions(self.repository, *results)

        def another_run_takes_dependent(request):
            if self.github.labels(DEPENDENT) == {"ready-for-agent"}:
                self.github.edit_issue_labels(
                    DEPENDENT, add="agent-working", remove=["ready-for-agent"]
                )
                if published:
                    self.github.comment_on_issue(
                        DEPENDENT, implementation_evidence(mode, self.repository.base_sha)["body"]
                    )

        self.sessions.before_session = another_run_takes_dependent
        return start_mode_run(mode, self.repository, self.github, self.sessions, PARENT)

    def assert_stopped_on_another_run(self, result, *, dependent_comments=0):
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.session_children(), [CHILD])
        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        self.assertEqual(self.github.labels(DEPENDENT), {"agent-working"})
        self.assertEqual(len(self.github.comments(DEPENDENT)), dependent_comments)
        summary = self.github.comments(PARENT)[-1]
        for published in (summary, result.stderr):
            self.assertIn("- final outcome: stopped safely", published)
            self.assertIn(
                f"Stop reason: #{DEPENDENT} labelled agent-working: another run on this "
                "parent is working on it",
                published,
            )
            self.assertIn(
                f"- #{DEPENDENT}: agent working in another run on this parent.", published
            )
            self.assertIn(
                f"- Another run is working on #{DEPENDENT}: find and stop the run that should "
                f"not be running, then run `macatia run {PARENT}` again if this one should "
                "continue.",
                published,
            )
            self.assertNotIn("Fix the stop reason", published)

    def test_run_stops_at_its_next_child_selection_without_another_session(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.add_issue(DEPENDENT, parent=PARENT)
        self.github.add_issue(LATER_CHILD, parent=PARENT)

        result = self.start_with_another_run(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_stopped_on_another_run(result)
        self.assertEqual(self.github.labels(LATER_CHILD), {"ready-for-agent"})

    def test_published_child_of_another_run_is_left_to_that_run(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.add_issue(DEPENDENT, parent=PARENT)

        result = self.start_with_another_run(
            loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN), published=True
        )

        self.assert_stopped_on_another_run(result, dependent_comments=1)

    def test_own_agent_working_child_does_not_stop_the_run(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.add_issue(DEPENDENT, parent=PARENT)

        result = self.start(loop_core.REVIEWED_RUN, *passing_child_results(loop_core.REVIEWED_RUN) * 2)

        self.assert_completed(result, sessions=4)
        self.assertEqual(self.session_children(), [CHILD, CHILD, DEPENDENT, DEPENDENT])


# Children implemented on the branch while CHILD waits in human review.
OTHER_CHILD = 195
LATER_CHILD = 196


class ChildBaseTests(RunLevelTestCase):
    """Each child cycle records its child base; a resumed child starts from the recorded one."""

    def child_bases(self, child):
        """The child base each trusted child cycle start comment records, in order."""
        return [
            json.loads(match.group(1))["child_base"]
            for comment in self.github.comments(child)
            if (match := re.search(
                re.escape(loop_core.CHILD_BASE_MARKER_PREFIX) + r"(\{.*?\}) -->", comment
            ))
        ]

    def start_comment(self, child):
        return next(
            comment for comment in reversed(self.github.comments(child))
            if is_child_cycle_start(comment)
        )

    def local_gate_bases(self):
        return [
            json.loads((evidence / "command.json").read_text())["base_sha"]
            for evidence in sorted((self.repository.diagnostics() / "evidence").glob("*-local-gate"))
        ]

    def forget_diagnostics(self):
        """Drop the finished run's diagnostics, so the next run's are the only ones."""
        shutil.rmtree(self.repository.diagnostics())

    def hand_off(self, mode, path):
        """Run one child cycle that commits `path` and hands the child to human review."""
        result = self.start(
            mode, ScriptedResult("blocked", "Needs a decision.", commit=True, path=path)
        )
        self.assertEqual(result.exit_code, 1, result.stderr)
        self.assertEqual(self.github.labels(CHILD), {"human-review"})
        self.forget_diagnostics()
        return self.repository.head()

    def relabel_ready(self):
        self.github.edit_issue_labels(CHILD, add="ready-for-agent", remove=["human-review"])

    def assert_evidence_starts_from(self, child_base):
        evidence = self.github.comments(CHILD)[-1]
        self.assertIn(f"Macatia verified implementation commits `{child_base}..", evidence)

    def test_child_cycle_start_records_the_child_base_after_marking_the_child_agent_working(self):
        self.github.add_issue(CHILD, parent=PARENT)
        labels_at_start = []

        def observe_labels_at_start(number, body):
            if is_child_cycle_start(body):
                labels_at_start.append(self.github.labels(number))

        self.github.after_comment = observe_labels_at_start

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        base_sha = self.repository.base_sha
        self.assertEqual(self.child_bases(CHILD), [base_sha])
        self.assertEqual(labels_at_start, [{"agent-working"}])
        start = self.start_comment(CHILD)
        self.assertIn(f"child base `{base_sha}`", start)
        self.assertNotIn("no longer on the branch", start)
        self.assertEqual(
            self.github.issues[CHILD]["comments"][0]["author"], {"login": TRUSTED_LOGIN}
        )

    def test_child_resumed_from_human_review_starts_from_its_first_child_base(self):
        self.github.add_issue(CHILD, parent=PARENT)
        base_sha = self.repository.base_sha
        self.hand_off(loop_core.REVIEWED_RUN, "first-cycle.txt")
        human_commit = self.commit_file("human-fix.txt", "Fix during human review")
        self.relabel_ready()
        self.hand_off(loop_core.REVIEWED_RUN, "second-cycle.txt")
        self.relabel_ready()

        result = self.start(
            loop_core.REVIEWED_RUN, ScriptedResult("committed"), ScriptedResult("no_changes")
        )

        self.assert_completed(result, sessions=2)
        implementation_head = self.repository.head()
        self.assertEqual(self.child_bases(CHILD), [base_sha] * 3)
        self.assert_evidence_starts_from(base_sha)
        self.assertIn(human_commit, self.repository.commits_since(base_sha))
        self.assertIn(
            f"Review the exact `{base_sha}...{implementation_head}` diff", self.sessions.prompts[1]
        )
        self.assertEqual(self.local_gate_bases(), [base_sha])

    def test_child_resumed_from_agent_working_starts_from_its_child_base(self):
        self.github.add_issue(CHILD, parent=PARENT)
        base_sha = self.repository.base_sha
        with self.assertRaises(RunInterrupted):
            self.start(loop_core.STANDARD_RUN, RunInterrupted("the run's process died"))
        self.assertEqual(self.github.labels(CHILD), {"agent-working"})
        self.commit_file("partial.txt", "Partial work of the interrupted child cycle")
        self.forget_diagnostics()

        result = self.start(
            loop_core.REVIEWED_RUN, ScriptedResult("committed"), ScriptedResult("no_changes"),
            confirm=ScriptedConfirm(RESUME),
        )

        self.assert_completed(result, sessions=2)
        self.assertEqual(self.child_bases(CHILD), [base_sha] * 2)
        self.assert_evidence_starts_from(base_sha)
        self.assertIn(
            f"Review the exact `{base_sha}...{self.repository.head()}` diff",
            self.sessions.prompts[1],
        )
        self.assertEqual(self.local_gate_bases(), [base_sha])

    def test_latest_trusted_child_base_off_the_branch_starts_from_the_current_head(self):
        old_base = self.repository.base_sha
        off_the_branch = self.commit_off_the_branch()
        head = self.commit_prior_child()
        self.github.add_issue(CHILD, parent=PARENT, comments=[
            child_base_marker(old_base),
            child_base_marker(off_the_branch),
            child_base_marker(old_base, author="outside-contributor"),
        ])

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.child_bases(CHILD)[-1], head)
        start = self.start_comment(CHILD)
        self.assertIn(f"child base `{head}`", start)
        self.assertIn(
            f"the recorded child base `{off_the_branch}` is no longer on the branch", start
        )
        self.assert_evidence_starts_from(head)

    def test_malformed_latest_trusted_child_base_starts_from_the_current_head(self):
        head = self.commit_prior_child()
        self.github.add_issue(CHILD, parent=PARENT, comments=[
            child_base_marker(self.repository.base_sha),
            {
                "author": {"login": TRUSTED_LOGIN},
                "body": f"Quoted by hand: {loop_core.CHILD_BASE_MARKER_PREFIX}{{\"child_base\"",
            },
        ])

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assertIn(
            "the latest child base marker is malformed", self.start_comment(CHILD)
        )
        self.assert_evidence_starts_from(head)

    def test_latest_trusted_child_base_that_is_not_a_full_commit_starts_from_the_current_head(self):
        head = self.commit_prior_child()
        self.github.add_issue(CHILD, parent=PARENT, comments=[
            child_base_marker(self.repository.base_sha),
            child_base_marker(self.repository.base_sha[:7]),
        ])

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assertIn(
            "the latest child base marker is malformed", self.start_comment(CHILD)
        )
        self.assert_evidence_starts_from(head)

    def test_untrusted_child_base_is_ignored(self):
        head = self.commit_prior_child()
        self.github.add_issue(CHILD, parent=PARENT, comments=[
            child_base_marker(self.repository.base_sha, author="outside-contributor"),
        ])

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assertNotIn("no longer on the branch", self.start_comment(CHILD))
        self.assert_evidence_starts_from(head)

    def test_child_base_older_than_the_reset_marker_starts_from_the_current_head(self):
        head = self.commit_prior_child()
        self.github.add_issue(CHILD, parent=PARENT, comments=[
            child_base_marker(self.repository.base_sha),
            reset_marker(),
        ])

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.child_bases(CHILD)[-1], head)
        self.assertNotIn("Macatia started from the current branch head", self.start_comment(CHILD))
        self.assert_evidence_starts_from(head)

    def test_child_base_after_the_reset_marker_or_before_an_untrusted_one_is_resumed(self):
        base_sha = self.repository.base_sha
        for name, comments in (
            ("child base after the reset marker", [reset_marker(), child_base_marker(base_sha)]),
            ("untrusted reset marker", [
                child_base_marker(base_sha), reset_marker(author="outside-contributor"),
            ]),
        ):
            with self.subTest(name):
                self.new_run()
                self.commit_prior_child()
                self.github.add_issue(CHILD, parent=PARENT, comments=comments)

                result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

                self.assert_completed(result, sessions=1)
                self.assertEqual(self.child_bases(CHILD)[-1], base_sha)
                self.assert_evidence_starts_from(base_sha)

    def test_started_child_stopped_by_a_dependency_deadlock_keeps_its_commits(self):
        self.commit_prior_child()
        self.github.add_issue(CHILD, parent=PARENT, blocked_by=[OTHER_CHILD], comments=[
            child_base_marker(self.repository.base_sha),
        ])
        self.github.add_issue(OTHER_CHILD, parent=PARENT, blocked_by=[CHILD])

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_stopped_before_any_session(result, "dependency deadlock")
        self.assertIn(COMMITS_STAY, self.github.comments(CHILD)[-1])
        self.assertNotIn(COMMITS_STAY, self.github.comments(OTHER_CHILD)[-1])

    def test_child_whose_child_base_left_the_branch_is_stopped_by_a_deadlock_without_commits(self):
        self.github.add_issue(CHILD, parent=PARENT, blocked_by=[OTHER_CHILD], comments=[
            child_base_marker(self.commit_off_the_branch()),
        ])
        self.github.add_issue(OTHER_CHILD, parent=PARENT, blocked_by=[CHILD])

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_stopped_before_any_session(result, "dependency deadlock")
        self.assertNotIn(COMMITS_STAY, self.github.comments(CHILD)[-1])

    def implement_other_child(self, mode):
        """A run that implements OTHER_CHILD on top of the branch while CHILD is in human review."""
        self.start(mode, *passing_child_results(mode))
        self.assertEqual(self.github.labels(OTHER_CHILD), {"implemented"})
        self.forget_diagnostics()
        return self.repository.head()

    def add_implemented_child(self, number, verified_head, *comments):
        self.github.add_issue(
            number, parent=PARENT, labels=("implemented",),
            comments=[*comments, implementation_evidence(loop_core.STANDARD_RUN, verified_head)],
        )

    def assert_names_uncovered_commits(self, uncovered_range):
        uncovered = f"`{uncovered_range}`"
        self.assertIn(uncovered, self.start_comment(CHILD))
        self.assertIn(uncovered, self.github.comments(CHILD)[-1])

    def test_child_resumed_after_another_child_was_implemented_on_top_moves_its_child_base(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.add_issue(OTHER_CHILD, parent=PARENT)
        base_sha = self.repository.base_sha
        self.hand_off(loop_core.REVIEWED_RUN, "a1.txt")
        self.relabel_ready()
        earlier_cycles_head = self.hand_off(loop_core.REVIEWED_RUN, "a2.txt")
        other_child_head = self.implement_other_child(loop_core.REVIEWED_RUN)
        human_commit = self.commit_file("human-fix.txt", "Fix during human review")
        self.relabel_ready()

        result = self.start(
            loop_core.REVIEWED_RUN, ScriptedResult("committed"), ScriptedResult("no_changes")
        )

        self.assert_completed(result, sessions=2)
        implementation_head = self.repository.head()
        uncovered = f"{base_sha}..{earlier_cycles_head}"
        self.assertEqual(self.child_bases(CHILD), [base_sha] * 3)
        self.assertIn(f"child base `{other_child_head}`", self.start_comment(CHILD))
        self.assert_evidence_starts_from(other_child_head)
        self.assert_names_uncovered_commits(uncovered)
        evidence = self.github.comments(CHILD)[-1]
        self.assertNotIn(f"`{earlier_cycles_head}..", evidence)
        self.assertIn(human_commit, self.repository.commits_since(other_child_head))
        review = self.sessions.prompts[1]
        self.assertIn(f"Review the exact `{other_child_head}...{implementation_head}` diff", review)
        self.assertIn(f"`{uncovered}`", review)
        self.assertEqual(self.local_gate_bases(), [base_sha])

    def test_child_base_moves_to_the_latest_child_implemented_after_the_recorded_one(self):
        self.github.add_issue(CHILD, parent=PARENT)
        base_sha = self.repository.base_sha
        earlier_cycles_head = self.hand_off(loop_core.STANDARD_RUN, "a1.txt")
        other_child_head = self.commit_file("other.txt", "Implement the other child")
        later_child_head = self.commit_file("later.txt", "Implement the later child")
        self.add_implemented_child(
            LATER_CHILD, later_child_head, child_base_marker(other_child_head)
        )
        self.add_implemented_child(
            OTHER_CHILD, other_child_head, child_base_marker(earlier_cycles_head)
        )
        self.relabel_ready()

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assert_evidence_starts_from(later_child_head)
        self.assert_names_uncovered_commits(f"{base_sha}..{earlier_cycles_head}")
        self.assertEqual(self.local_gate_bases(), [base_sha])

    def test_child_cycles_between_other_children_are_named_as_one_inexact_range(self):
        self.github.add_issue(CHILD, parent=PARENT)
        base_sha = self.repository.base_sha
        first_cycle_head = self.hand_off(loop_core.STANDARD_RUN, "a1.txt")
        other_child_head = self.commit_file("other.txt", "Implement the other child")
        self.add_implemented_child(
            OTHER_CHILD, other_child_head, child_base_marker(first_cycle_head)
        )
        self.relabel_ready()
        second_cycle_head = self.hand_off(loop_core.STANDARD_RUN, "a2.txt")
        later_child_head = self.commit_file("later.txt", "Implement the later child")
        self.add_implemented_child(
            LATER_CHILD, later_child_head, child_base_marker(second_cycle_head)
        )
        self.relabel_ready()

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assert_evidence_starts_from(later_child_head)
        self.assert_names_uncovered_commits(f"{base_sha}..{second_cycle_head}")
        self.assertIn("and other children's work", self.start_comment(CHILD))
        self.assertEqual(self.local_gate_bases(), [base_sha])

    def test_child_cycle_after_another_child_is_named_when_the_first_left_no_commits(self):
        self.github.add_issue(CHILD, parent=PARENT)
        base_sha = self.repository.base_sha
        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("blocked", "Needs a decision."))
        self.assertEqual(self.github.labels(CHILD), {"human-review"}, result.stderr)
        self.forget_diagnostics()
        other_child_head = self.commit_file("other.txt", "Implement the other child")
        self.add_implemented_child(OTHER_CHILD, other_child_head, child_base_marker(base_sha))
        self.relabel_ready()
        second_cycle_head = self.hand_off(loop_core.STANDARD_RUN, "a2.txt")
        later_child_head = self.commit_file("later.txt", "Implement the later child")
        self.add_implemented_child(
            LATER_CHILD, later_child_head, child_base_marker(second_cycle_head)
        )
        self.relabel_ready()

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assert_evidence_starts_from(later_child_head)
        self.assert_names_uncovered_commits(f"{other_child_head}..{second_cycle_head}")
        self.assertIn("from this child's earlier child cycles", self.start_comment(CHILD))
        self.assertEqual(self.local_gate_bases(), [base_sha])

    def test_other_child_without_a_recorded_child_base_names_every_commit_before_the_child_base(self):
        self.github.add_issue(CHILD, parent=PARENT)
        base_sha = self.repository.base_sha
        self.hand_off(loop_core.STANDARD_RUN, "a1.txt")
        other_child_head = self.commit_file("other.txt", "Implement the other child")
        self.add_implemented_child(OTHER_CHILD, other_child_head)
        self.relabel_ready()

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assert_evidence_starts_from(other_child_head)
        self.assert_names_uncovered_commits(f"{base_sha}..{other_child_head}")

    def test_child_implemented_before_the_recorded_child_base_does_not_move_it(self):
        prior = self.commit_prior_child()
        self.add_implemented_child(OTHER_CHILD, prior)
        self.github.add_issue(CHILD, parent=PARENT)
        self.hand_off(loop_core.STANDARD_RUN, "a1.txt")
        self.relabel_ready()

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assert_evidence_starts_from(prior)
        self.assertNotIn("not covered", self.start_comment(CHILD))
        self.assertNotIn("not covered", self.github.comments(CHILD)[-1])
        self.assertEqual(self.local_gate_bases(), [prior])


class RunUsageCommandLineTests(unittest.TestCase):
    """`macatia run` usage errors name the program as invoked, with the review flags given."""

    def usage_error(self, *arguments):
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "dont_write_bytecode", sys.dont_write_bytecode),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as exit,
        ):
            cli.main(["run", *arguments])
        self.assertEqual(exit.exception.code, 2)
        return stderr.getvalue()

    def test_standard_run_usage_names_macatia_run(self):
        for arguments in (("0",), ("--caffeinate", "abc")):
            with self.subTest(arguments=arguments):
                error = self.usage_error(*arguments)
                self.assertTrue(error.startswith("usage: macatia run [-h]"), error)
                self.assertIn("must be a positive integer", error)

    def test_reviewed_run_usage_names_macatia_run_review(self):
        for arguments in (("--review", "0"), ("0", "--review", "--caffeinate")):
            with self.subTest(arguments=arguments):
                error = self.usage_error(*arguments)
                self.assertTrue(error.startswith("usage: macatia run --review [-h]"), error)
                self.assertIn("must be a positive integer", error)

    def test_standard_run_usage_names_macatia_run_no_review(self):
        error = self.usage_error("0", "--no-review")
        self.assertTrue(error.startswith("usage: macatia run --no-review [-h]"), error)
        self.assertIn("must be a positive integer", error)

    def test_usage_shows_the_issue_as_optional(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["--help"]), 0)
        self.assertIn(
            "run [--review | --no-review] [--caffeinate | --no-caffeinate] [--tmux] [--worktree] "
            "[--config PATH] [ISSUE]\n",
            stdout.getvalue(),
        )
        self.assertIn("--tmux asks", stdout.getvalue())
        self.assertIn("--worktree offers", stdout.getvalue())
        self.assertIn("<repository>.worktrees/issue-<run target>", stdout.getvalue())
        self.assertIn("macatia-<repository name>-<run target>", stdout.getvalue())
        self.assertNotIn("budget", stdout.getvalue().lower())
        self.assertIn(
            "Without ISSUE, at a terminal, offers the startable run targets to choose from.\n",
            stdout.getvalue(),
        )

    def test_run_usage_names_the_issue(self):
        error = self.usage_error("abc")
        self.assertIn("usage: macatia run [-h] [--config CONFIG] [ISSUE]\n", error)
        self.assertIn("argument ISSUE: must be a positive integer", error)

    def test_a_second_number_is_an_unrecognized_argument(self):
        error = self.usage_error("123", "10")
        self.assertIn("usage: macatia run [-h] [--config CONFIG] [ISSUE]\n", error)
        self.assertIn("error: unrecognized arguments: 10", error)

    def test_review_and_no_review_together_are_a_usage_error(self):
        for arguments in (("--review", "--no-review", "5"), ("5", "--no-review", "--caffeinate", "--review")):
            with self.subTest(arguments=arguments):
                error = self.usage_error(*arguments)
                self.assertIn("argument --no-review: not allowed with argument --review", error)


class BakeCommandLineTests(unittest.TestCase):
    """`macatia bake` is an undocumented alias of `macatia run`."""

    def run_call(self, command):
        with (
            mock.patch.object(sys.stdin, "isatty", return_value=False),
            mock.patch.object(loop, "run", return_value=0) as run,
            mock.patch.object(sys, "dont_write_bytecode", sys.dont_write_bytecode),
        ):
            self.assertEqual(cli.main([command, "--review", "5"]), 0)
        return run.call_args

    def test_bake_runs_like_run(self):
        self.assertEqual(self.run_call("bake"), self.run_call("run"))

    def test_usage_leaves_bake_out(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["--help"]), 0)
        self.assertNotIn("bake", stdout.getvalue())


class StartupChoiceCommandLineTests(unittest.TestCase):
    """`macatia run` asks the startup question with the arrow keys only in a terminal."""

    def adapters_passed(self, *, stdin_tty, stdout_tty, review=False):
        """The startup choice and run target picker `macatia run` passes to the run."""
        with (
            mock.patch.object(sys.stdin, "isatty", return_value=stdin_tty),
            mock.patch.object(sys.stdout, "isatty", return_value=stdout_tty),
            mock.patch.object(loop, "run", return_value=0) as run,
            mock.patch.object(sys, "dont_write_bytecode", sys.dont_write_bytecode),
        ):
            self.assertEqual(cli.main(["run", *(["--review"] if review else []), "5"]), 0)
        kwargs = run.call_args.kwargs
        return kwargs["confirm"], kwargs["choose_run_target"], kwargs["choose_continuation"]

    def test_both_terminals_get_the_arrow_key_selector(self):
        for review in (False, True):
            with self.subTest(review=review):
                self.assertEqual(
                    self.adapters_passed(stdin_tty=True, stdout_tty=True, review=review),
                    (
                        cli.startup_choice_with_arrows, cli.run_target_with_arrows,
                        cli.continuation_with_arrows,
                    ),
                )

    def test_without_both_terminals_there_is_no_selector(self):
        for stdin_tty, stdout_tty in ((False, True), (True, False), (False, False)):
            with self.subTest(stdin_tty=stdin_tty, stdout_tty=stdout_tty):
                self.assertEqual(
                    self.adapters_passed(stdin_tty=stdin_tty, stdout_tty=stdout_tty),
                    (None, None, None),
                )

    def test_run_target_selector_offers_the_entries_and_returns_the_picked_number(self):
        targets = [
            loop_core.StartableRunTarget(12, "Add a cart", 3),
            loop_core.StartableRunTarget(20, "Fix login", None),
        ]
        for preselected, default in ((None, "#12  Add a cart  (parent · 3 ready children)"),
                                     (20, "#20  Fix login  (standalone)")):
            with (
                self.subTest(preselected=preselected),
                mock.patch.object(
                    check, "arrow_select", return_value="#20  Fix login  (standalone)"
                ) as select,
            ):
                self.assertEqual(cli.run_target_with_arrows(targets, preselected), 20)
                select.assert_called_once_with(
                    "Which run target should Macatia run?",
                    [("#12  Add a cart  (parent · 3 ready children)", ""),
                     ("#20  Fix login  (standalone)", "")],
                    default,
                )

    def test_ctrl_c_in_the_run_target_selector_reaches_the_run(self):
        targets = [loop_core.StartableRunTarget(12, "Add a cart", 1)]
        with (
            mock.patch.object(check, "arrow_select", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            cli.run_target_with_arrows(targets, None)

    def test_continuation_selector_offers_restart_by_default_with_descriptions(self):
        name = "macatia-buyr-5"
        for choice, continuation in (
            (f"Restart in {name}", Continuation.RESTART),
            ("Continue here", Continuation.CONTINUE_HERE),
            ("Stop", Continuation.STOP),
        ):
            with (
                self.subTest(choice=choice),
                mock.patch.object(check, "arrow_select", return_value=choice) as select,
            ):
                self.assertIs(cli.continuation_with_arrows(name), continuation)
                select.assert_called_once_with(
                    "Where should this run continue?",
                    [
                        (f"Restart in {name}",
                         "the run restarts in a persistent terminal and attaches to it"),
                        ("Continue here", "the run continues in this terminal"),
                        ("Stop", "the run stops with nothing changed"),
                    ],
                    f"Restart in {name}",
                )

    def test_ctrl_c_in_the_continuation_selector_reaches_the_run(self):
        with (
            mock.patch.object(check, "arrow_select", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            cli.continuation_with_arrows("macatia-buyr-5")

    def test_selector_offers_stop_by_default_and_resumes_only_on_resume(self):
        for choice, resume in (("Stop", False), ("Resume", True)):
            with (
                self.subTest(choice=choice),
                mock.patch.object(check, "arrow_select", return_value=choice) as select,
            ):
                self.assertIs(
                    cli.startup_choice_with_arrows("Resume #193?", "Resume", accept_by_default=False),
                    resume,
                )
                select.assert_called_once_with(
                    "Resume #193?", [("Stop", ""), ("Resume", "")], "Stop"
                )

    def test_selector_can_offer_the_accepting_choice_by_default(self):
        question = "Create macatia/issue-5 from an up-to-date main?"
        for choice, create in (("Create", True), ("Stop", False)):
            with (
                self.subTest(choice=choice),
                mock.patch.object(check, "arrow_select", return_value=choice) as select,
            ):
                self.assertIs(
                    cli.startup_choice_with_arrows(question, "Create", accept_by_default=True),
                    create,
                )
                select.assert_called_once_with(
                    question, [("Create", ""), ("Stop", "")], "Create"
                )


class ParentLabelTestCase(RunLevelTestCase):
    def record_parent_label_edits(self):
        """Record each label edit on the parent as (added, removed) from now on."""
        self.parent_edits = []

        def record(number, add, remove):
            if number == PARENT:
                self.parent_edits.append((add, sorted(remove)))

        self.github.after_label_edit = record

    def observe_parent_labels(self):
        """Record the parent's labels as each agent session starts."""
        self.observed_parent_labels = []
        self.before_session = lambda request: self.observed_parent_labels.append(
            self.github.labels(PARENT)
        )


class ParentCompletionTests(ParentLabelTestCase):
    def test_all_children_complete_hands_the_parent_to_human_review_without_sessions(self):
        self.new_run(parent_labels=("ready-for-agent", "implemented", "documentation"))
        self.github.add_issue(CLOSED_BLOCKER, parent=PARENT, state="closed", labels=())
        self.github.add_issue(CHILD, parent=PARENT, labels=("implemented",))
        self.record_parent_label_edits()

        result = self.start(loop_core.STANDARD_RUN)

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(
            self.parent_edits, [("human-review", ["implemented", "ready-for-agent"])]
        )
        self.assertIn("- agent sessions: 0\n- infrastructure retries: 0\n", result.stdout)
        self.assertIn("implemented children this run: 0", result.stdout)
        self.assertEqual(self.github.labels(PARENT), {"human-review", "documentation"})
        parent_comments = self.github.comments(PARENT)
        self.assertIn("<!-- macatia-experimental-parent-completion -->", parent_comments[-2])
        self.assertIn("all 2 children complete", parent_comments[-2])
        self.assertIn("- final outcome: completed", parent_comments[-1])

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

    def test_parent_whose_reset_marker_is_newer_than_its_handoff_is_handed_off_again(self):
        for mode in MODES:
            for marker in (mode.parent_completion_marker, mode.terminal_summary_marker):
                with self.subTest(mode=mode.diagnostics_label, marker=marker):
                    self.new_run()
                    self.github.add_issue(CHILD, parent=PARENT, state="closed", labels=())
                    # Both posted in the same second, so the handoff's time is still the
                    # parent's latest update.
                    updated_at = self.github.issues[PARENT]["updatedAt"]
                    self.github.issues[PARENT]["comments"] = [
                        {"author": {"login": TRUSTED_LOGIN}, "body": f"{marker}\nHanded off.", "createdAt": updated_at},
                        reset_marker(created_at=updated_at),
                    ]

                    result = self.start(mode)

                    self.assert_completed(result, sessions=0)
                    self.assertIn(mode.parent_completion_marker, self.github.comments(PARENT)[-2])

    def test_untrusted_reset_marker_leaves_the_parent_handed_off(self):
        self.github.add_issue(CHILD, parent=PARENT, state="closed", labels=())
        updated_at = self.github.issues[PARENT]["updatedAt"]
        marker = loop_core.STANDARD_RUN.parent_completion_marker
        self.github.issues[PARENT]["comments"] = [
            {"author": {"login": TRUSTED_LOGIN}, "body": f"{marker}\nHanded off.", "createdAt": updated_at},
            reset_marker(author="outside-contributor", created_at=updated_at),
        ]

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_completed(result, sessions=0)
        self.assertEqual(sum(marker in comment for comment in self.github.comments(PARENT)), 1)

    def test_completion_removes_work_started_from_the_parent(self):
        for parent_labels in (("work-started",), ("human-review", "work-started", "documentation")):
            with self.subTest(parent_labels=parent_labels):
                self.new_run(parent_labels=parent_labels)
                self.github.add_issue(CHILD, parent=PARENT, labels=("implemented",))

                result = self.start(loop_core.STANDARD_RUN)

                self.assertEqual(result.exit_code, 0, result.stderr)
                self.assertEqual(
                    self.github.labels(PARENT),
                    {"human-review"} | (set(parent_labels) - {"work-started"}),
                )


class ParentWorkStartedTests(ParentLabelTestCase):
    """The first child cycle of a run labels the parent work started instead of ready for agent."""

    def test_first_child_cycle_marks_the_parent_work_started_in_one_edit(self):
        self.new_run(parent_labels=("ready-for-agent", "documentation", "human-review"))
        self.github.add_issue(CHILD, parent=PARENT)
        self.record_parent_label_edits()
        self.observe_parent_labels()

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(
            self.observed_parent_labels, [{"work-started", "documentation", "human-review"}]
        )
        self.assertEqual(
            self.parent_edits,
            [("work-started", ["ready-for-agent"]), ("human-review", ["work-started"])],
        )
        self.assertEqual(self.github.labels(PARENT), {"human-review", "documentation"})

    def test_configuration_from_before_work_started_marks_the_parent_with_the_default_label(self):
        self.repository.edit_configuration('work_started = "work-started"\n', "")
        self.github.add_issue(CHILD, parent=PARENT)
        self.record_parent_label_edits()

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assertEqual(
            self.parent_edits,
            [("work-started", ["ready-for-agent"]), ("human-review", ["work-started"])],
        )

    def test_later_children_of_the_same_run_make_no_parent_label_write(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.add_issue(DEPENDENT, parent=PARENT)
        self.record_parent_label_edits()

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN) * 2)

        self.assert_completed(result, sessions=2)
        self.assertEqual(
            self.parent_edits,
            [("work-started", ["ready-for-agent"]), ("human-review", ["work-started"])],
        )

    def test_later_run_on_a_work_started_parent_makes_no_parent_label_write(self):
        self.new_run(parent_labels=("work-started", "documentation"))
        self.github.add_issue(CHILD, parent=PARENT)
        self.record_parent_label_edits()
        self.observe_parent_labels()

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(self.observed_parent_labels, [{"work-started", "documentation"}])
        self.assertEqual(self.parent_edits, [("human-review", ["work-started"])])

    def test_resumed_run_on_a_work_started_parent_makes_no_parent_label_write(self):
        self.new_run(parent_labels=("work-started",))
        self.github.add_issue(CHILD, parent=PARENT, labels=("agent-working",))
        self.record_parent_label_edits()

        result = self.start(
            loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN),
            confirm=ScriptedConfirm(RESUME),
        )

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.parent_edits, [("human-review", ["work-started"])])

    def test_run_stopped_on_a_child_handed_to_human_review_leaves_the_parent_work_started(self):
        self.github.add_issue(CHILD, parent=PARENT)

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("blocked", "Needs a product decision."))

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.github.labels(CHILD), {"human-review"})
        self.assertEqual(self.github.labels(PARENT), {"work-started"})

    def test_interrupted_run_leaves_the_parent_work_started(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.interrupt_after_comment_on = CHILD
        self.github.interrupt_after_comment_containing = (
            loop_core.STANDARD_RUN.implementation_evidence_prefix
        )

        with self.assertRaises(RunInterrupted):
            self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assertEqual(self.github.labels(PARENT), {"work-started"})

    def test_run_stopped_before_any_child_cycle_leaves_the_parent_labels_untouched(self):
        for confirm in (ScriptedConfirm(STOP), None):
            with self.subTest(confirm=confirm):
                self.new_run(parent_labels=("ready-for-agent", "documentation"))
                self.github.add_issue(CHILD, parent=PARENT, labels=("agent-working",))
                self.record_parent_label_edits()

                result = self.start(loop_core.STANDARD_RUN, confirm=confirm)

                self.assert_stopped_before_any_session(result, "labelled agent-working")
                self.assertEqual(self.parent_edits, [])
                self.assertEqual(self.github.labels(PARENT), {"ready-for-agent", "documentation"})

    def test_run_stopped_on_another_run_before_any_child_cycle_leaves_the_parent_labels_untouched(self):
        self.github.add_issue(CHILD, parent=PARENT, labels=("agent-working",))
        self.record_parent_label_edits()
        record_parent_edit = self.github.after_label_edit

        def another_run_takes_the_resumed_child(number, add, remove):
            record_parent_edit(number, add, remove)
            if number == CHILD and add == "ready-for-agent":
                self.github.edit_issue_labels(CHILD, add="agent-working", remove=["ready-for-agent"])

        self.github.after_label_edit = another_run_takes_the_resumed_child

        result = self.start(loop_core.STANDARD_RUN, confirm=ScriptedConfirm(RESUME))

        self.assert_stopped_before_any_session(result, "another run on this parent is working on it")
        self.assertEqual(self.parent_edits, [])
        self.assertEqual(self.github.labels(PARENT), {"ready-for-agent"})

    def test_failed_work_started_write_warns_and_the_run_continues(self):
        self.github.add_issue(CHILD, parent=PARENT)

        # The parent write is the next label edit after the child is marked agent working.
        def fail_the_parent_write(number, add, remove):
            if number == CHILD and add == "agent-working":
                self.github.label_edit_error = MacatiaError("gh issue edit failed: HTTP 502")

        self.github.after_label_edit = fail_the_parent_write

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        warning = (
            f"Macatia could not label parent #{PARENT} work-started: gh issue edit failed: HTTP 502."
        )
        self.assertIn(f"⚠️  {warning}", result.stdout)
        summary = self.github.comments(PARENT)[-1]
        self.assertIn("- final outcome: completed", summary)
        for printed in (summary, result.stdout.split("🏁 Macatia summary")[-1]):
            self.assertIn(f"- warning: {warning}", printed)

    def test_parent_and_children_never_get_the_standalone_marker(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.add_issue(DEPENDENT, parent=PARENT)
        added = []
        self.github.after_label_edit = lambda number, add, remove: added.append(add)

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN) * 2)

        self.assert_completed(result, sessions=2)
        self.assertNotIn("standalone", added)
        for number in (PARENT, CHILD, DEPENDENT):
            self.assertNotIn("standalone", self.github.labels(number))


class RepeatedRunTests(RunLevelTestCase):
    def test_second_run_in_the_same_process_does_not_share_the_first_runs_state(self):
        self.github.add_issue(CHILD, parent=PARENT)
        first = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))
        self.assert_completed(first, sessions=1)

        second = self.start(loop_core.STANDARD_RUN)

        self.assert_completed(second, sessions=0)
        self.assertEqual(self.sessions.requests, [])
        first_summary, second_summary = (
            comment for comment in self.github.comments(PARENT)
            if loop_core.STANDARD_RUN.terminal_summary_marker in comment
        )
        self.assertIn("- implemented children this run: 1\n", first_summary)
        self.assertIn("- agent sessions: 1\n", first_summary)
        self.assertIn("- implemented children this run: 0\n", second_summary)
        self.assertIn("- agent sessions: 0\n", second_summary)
        runs = list((self.repository.path / ".macatia" / "diagnostics").glob("*/run.json"))
        self.assertEqual(len(runs), 2)


class RunDiagnosticsTests(RunLevelTestCase):
    """The repository's own ignore rules say nothing about `.macatia/`."""

    def setUp(self):
        super().setUp()
        self.github.add_issue(CHILD, parent=PARENT)
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", ".macatia/"], cwd=self.repository.path, check=False
        )
        self.assertEqual(ignored.returncode, 1, "the fixture must not ignore .macatia/")

    def test_run_diagnostics_never_make_the_worktree_dirty(self):
        # Real session handling records each session in `.macatia/` and checks the worktree after it.
        engine = ScriptedEngine(self.repository, EngineResult(), EngineResult())
        result = start_mode_run(loop_core.REVIEWED_RUN, self.repository, self.github, engine, PARENT)

        self.assert_completed(result, sessions=2)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        self.assertEqual(len(self.repository.session_directories()), 2)
        self.assertTrue(self.repository.is_clean())

    def test_maintainer_files_in_the_diagnostics_directory_are_kept(self):
        own = {".gitignore": "# Kept by the maintainer\n*\n", "notes.txt": "Mine.\n"}
        for name, text in own.items():
            path = self.repository.path / ".macatia" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(text, encoding="utf-8")

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

        self.assert_completed(result, sessions=1)
        for name, text in own.items():
            self.assertEqual((self.repository.path / ".macatia" / name).read_text(encoding="utf-8"), text)
        self.assertTrue(self.repository.is_clean())


class StartupRejectionTestCase(RunLevelTestCase):
    """One ready child under the parent; each test breaks one startup condition."""

    def setUp(self):
        super().setUp()
        self.github.add_issue(CHILD, parent=PARENT)

    def assert_rejected_at_startup(self, result, reason):
        """The run stopped before reading any issue: nothing was commented, labelled, or pushed."""
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn(reason, result.stderr)
        self.assertEqual(self.sessions.requests, [])
        self.assertEqual(self.github.comments(PARENT), [])
        self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})
        self.assertIsNone(self.github.pull_request())
        with self.assertRaises(subprocess.CalledProcessError):
            self.repository.pushed_head()


class StartupTests(StartupRejectionTestCase):
    def test_detached_head_is_rejected_before_any_session(self):
        git(self.repository.path, "checkout", "--detach", self.repository.base_sha)

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_rejected_at_startup(result, "detached HEAD is not allowed")

    def test_dirty_worktree_is_rejected_before_any_session(self):
        (self.repository.path / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_rejected_at_startup(result, "worktree is dirty at startup")

    def test_checkout_that_is_not_the_selected_worktree_is_rejected_before_any_session(self):
        for name, directory in (
            ("outside the repository", self.repository.path.parent),
            ("inside the repository", self.repository.path / ".github"),
        ):
            with self.subTest(name):
                self.sessions = ScriptedSessions(self.repository)

                result = start_mode_run(
                    loop_core.STANDARD_RUN, self.repository, self.github, self.sessions, PARENT, cwd=directory
                )

                self.assert_rejected_at_startup(result, "wrong worktree")

    def test_wrong_repository_is_rejected_before_any_session(self):
        self.github.repository = "someone/else"

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_rejected_at_startup(
            result, "wrong repository: expected lmpaul/buyr, found someone/else"
        )

    def test_child_not_ready_for_an_agent_is_rejected_before_any_session(self):
        self.github.edit_issue_labels(CHILD, "needs-info", ())

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_stopped_before_any_session(
            result, f"parent issue #{PARENT} has children remaining but none is runnable"
        )
        self.assertEqual(self.github.labels(CHILD), {"ready-for-agent", "needs-info"})

    def test_arguments_must_be_positive_integers(self):
        for arguments in (("0",), ("abc",)):
            with self.subTest(arguments=arguments):
                self.sessions = ScriptedSessions(self.repository)

                result = start_mode_run(loop_core.STANDARD_RUN, self.repository, self.github, self.sessions, *arguments)

                self.assert_rejected_at_startup(result, "must be a positive integer")

    def test_without_a_run_target_or_its_picker_the_run_asks_for_the_issue_before_reading_github(self):
        for name, confirm, terminal in (
            ("without a terminal", None, False),
            # Only the run target picker lets a run start without its run target.
            ("without a run target picker", ScriptedConfirm(RESUME), True),
        ):
            for flags in ((), ("--review",), ("--no-review", "--caffeinate")):
                with self.subTest(name, flags=flags):
                    self.sessions = ScriptedSessions(self.repository)

                    result = start_run(
                        self.repository, self.github, self.sessions, *flags,
                        confirm=confirm, terminal=terminal,
                    )

                    self.assertEqual(result.exit_code, 2)
                    self.assert_rejected_at_startup(result, "pass the issue to run: macatia run ISSUE")
                    self.assertEqual(self.github.opened_for, [])
                    self.assertEqual(self.github.labels(PARENT), {"ready-for-agent"})
                    if confirm is not None:
                        self.assertEqual(confirm.questions, [])


RUN_BRANCH = f"macatia/issue-{PARENT}"


class BaseBranchTestCase(StartupRejectionTestCase):
    """A run started on the base branch, with a second clone that can push to `origin`."""

    def setUp(self):
        super().setUp()
        git(self.repository.path, "checkout", "main")
        self.base_head = self.repository.head()

    def push_to_origin_main(self, name):
        """Another clone pushes a commit to `origin`'s main; returns its sha."""
        clone = self.repository.path.parent / "other-clone"
        if not clone.exists():
            git(self.repository.path.parent, "clone", "-q", "-b", "main", str(self.repository.origin), str(clone))
            git(clone, "config", "user.email", "other@example.com")
            git(clone, "config", "user.name", "Other")
        (clone / name).write_text(f"{name}\n", encoding="utf-8")
        git(clone, "add", name)
        git(clone, "commit", "-m", f"Add {name}")
        git(clone, "push", "-q", "origin", "main")
        return git(clone, "rev-parse", "HEAD")

    def commit_locally(self, name):
        (self.repository.path / name).write_text(f"{name}\n", encoding="utf-8")
        git(self.repository.path, "add", name)
        git(self.repository.path, "commit", "-m", f"Add {name}")
        return self.repository.head()

    def current_branch(self):
        return git(self.repository.path, "branch", "--show-current")

    def origin_branches(self):
        return git(self.repository.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()

    def local_branches(self):
        return git(self.repository.path, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()

    def assert_nothing_changed(self, result, reason, *, base_head=None):
        """The run stopped on the base branch: nothing pulled, created, pushed, or published."""
        self.assert_rejected_at_startup(result, reason)
        self.assertEqual(self.current_branch(), "main")
        self.assertEqual(self.repository.head(), base_head or self.base_head)
        self.assertNotIn(RUN_BRANCH, self.local_branches())
        self.assertNotIn(RUN_BRANCH, self.origin_branches())

class RunBranchTests(BaseBranchTestCase):
    """A run started on the base branch offers to create the parent's run branch."""

    def test_create_fast_forwards_the_base_branch_and_pushes_the_run_branch_before_any_session(self):
        remote_head = self.push_to_origin_main("upstream.txt")
        pushed_at_first_session = []
        self.before_session = lambda request: pushed_at_first_session.append(
            (self.current_branch(), self.repository.pushed_head(RUN_BRANCH),
             git(self.repository.path, "rev-parse", "--abbrev-ref", "@{upstream}"))
        )
        confirm = ScriptedConfirm(CREATE)

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"), confirm=confirm)

        self.assert_completed(result, sessions=1)
        self.assertEqual(confirm.questions, [f"Create {RUN_BRANCH} from an up-to-date main?"])
        self.assertEqual(confirm.choices, [("Create", True)])
        self.assertIn("main is the base branch", result.stdout)
        self.assertEqual(pushed_at_first_session, [(RUN_BRANCH, remote_head, f"origin/{RUN_BRANCH}")])
        self.assertEqual(git(self.repository.path, "rev-parse", "main"), remote_head)
        self.assertEqual(self.current_branch(), RUN_BRANCH)
        self.assertEqual(self.repository.pushed_head(RUN_BRANCH), self.repository.head())
        pull = self.github.pull_request()
        self.assertEqual((pull.base, pull.head), ("main", RUN_BRANCH))
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_stop_and_ctrl_c_change_nothing(self):
        self.push_to_origin_main("upstream.txt")
        for answer in (STOP, KeyboardInterrupt()):
            with self.subTest(answer=answer):
                confirm = ScriptedConfirm(answer)

                result = self.start(loop_core.STANDARD_RUN, confirm=confirm)

                self.assert_nothing_changed(result, RUN_BRANCH)
                self.assertEqual(len(confirm.questions), 1)

    def test_without_a_terminal_the_run_stops_and_names_the_branch_to_create(self):
        result = self.start(loop_core.STANDARD_RUN, confirm=None)

        self.assert_nothing_changed(result, f"create {RUN_BRANCH}")

    def test_unstartable_parent_stops_before_any_branch_question(self):
        self.github.edit_issue_labels(PARENT, "needs-info", ())
        confirm = ScriptedConfirm(CREATE)

        result = self.start(loop_core.STANDARD_RUN, confirm=confirm)

        self.assert_nothing_changed(result, "held back by triage: needs-info")
        self.assertEqual(confirm.questions, [])

    def test_dirty_worktree_stops_before_any_branch_question(self):
        (self.repository.path / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
        confirm = ScriptedConfirm(CREATE)

        result = self.start(loop_core.STANDARD_RUN, confirm=confirm)

        self.assert_nothing_changed(result, "worktree is dirty at startup")
        self.assertEqual(confirm.questions, [])

    def test_unreachable_remote_stops_before_asking(self):
        git(self.repository.path, "remote", "set-url", "origin", str(self.repository.path.parent / "missing.git"))
        confirm = ScriptedConfirm(CREATE)

        result = self.start(loop_core.STANDARD_RUN, confirm=confirm)

        self.assert_rejected_at_startup(result, "could not reach origin")
        self.assertEqual(confirm.questions, [])
        self.assertEqual(self.current_branch(), "main")
        self.assertNotIn(RUN_BRANCH, self.local_branches())

    def test_base_branch_that_cannot_fast_forward_stops_with_no_branch_created(self):
        for name, diverge, reason in (
            ("local commits", False, "main has commits that are not on origin/main"),
            ("diverged", True, "main cannot fast-forward to origin/main"),
        ):
            with self.subTest(name):
                self.new_run()
                self.github.add_issue(CHILD, parent=PARENT)
                git(self.repository.path, "checkout", "main")
                if diverge:
                    self.push_to_origin_main("upstream.txt")
                local_head = self.commit_locally("local.txt")

                result = self.start(loop_core.STANDARD_RUN, confirm=ScriptedConfirm(CREATE))

                self.assert_nothing_changed(result, reason, base_head=local_head)

    def test_failed_push_stops_and_keeps_the_local_run_branch(self):
        hook = self.repository.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'pushes are refused' >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

        result = self.start(loop_core.STANDARD_RUN, confirm=ScriptedConfirm(CREATE))

        self.assert_rejected_at_startup(result, f"could not push {RUN_BRANCH}")
        self.assertEqual(self.current_branch(), RUN_BRANCH)
        self.assertNotIn(RUN_BRANCH, self.origin_branches())

    def existing_run_branch(self, *, pushed=False):
        """An earlier run left the run branch behind with a commit of its own; returns its head."""
        git(self.repository.path, "switch", "-q", "--create", RUN_BRANCH)
        head = self.commit_locally("earlier-run.txt")
        if pushed:
            git(self.repository.path, "push", "-q", "--set-upstream", "origin", RUN_BRANCH)
        git(self.repository.path, "switch", "-q", "main")
        return head

    def upstream(self):
        return git(self.repository.path, "rev-parse", "--abbrev-ref", "@{upstream}")

    def test_switch_moves_to_an_existing_local_branch_and_pushes_it_before_any_session(self):
        run_branch_head = self.existing_run_branch()
        self.push_to_origin_main("upstream.txt")
        at_first_session = []
        self.before_session = lambda request: at_first_session.append(
            (self.current_branch(), self.repository.head(),
             self.repository.pushed_head(RUN_BRANCH), self.upstream())
        )
        confirm = ScriptedConfirm(SWITCH)

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"), confirm=confirm)

        self.assert_completed(result, sessions=1)
        self.assertEqual(confirm.questions, [f"Switch to the existing {RUN_BRANCH}?"])
        self.assertEqual(confirm.choices, [("Switch", True)])
        self.assertIn("main is the base branch", result.stdout)
        self.assertEqual(
            at_first_session, [(RUN_BRANCH, run_branch_head, run_branch_head, f"origin/{RUN_BRANCH}")]
        )
        self.assertEqual(git(self.repository.path, "rev-parse", "main"), self.base_head)
        self.assertEqual(self.current_branch(), RUN_BRANCH)
        self.assertEqual(len(self.repository.commits_since(run_branch_head)), 1)
        pull = self.github.pull_request()
        self.assertEqual((pull.base, pull.head), ("main", RUN_BRANCH))

    def test_switch_to_a_branch_already_on_origin_proceeds(self):
        for name, ahead in (("in step", False), ("ahead of origin", True)):
            with self.subTest(name):
                self.new_run()
                self.github.add_issue(CHILD, parent=PARENT)
                git(self.repository.path, "checkout", "main")
                run_branch_head = self.existing_run_branch(pushed=True)
                if ahead:
                    git(self.repository.path, "switch", "-q", RUN_BRANCH)
                    run_branch_head = self.commit_locally("unpushed.txt")
                    git(self.repository.path, "switch", "-q", "main")
                at_first_session = []
                self.before_session = lambda request, seen=at_first_session: seen.append(
                    (self.repository.head(), self.repository.pushed_head(RUN_BRANCH), self.upstream())
                )

                result = self.start(
                    loop_core.STANDARD_RUN, ScriptedResult("committed"), confirm=ScriptedConfirm(SWITCH)
                )

                self.assert_completed(result, sessions=1)
                self.assertEqual(
                    at_first_session, [(run_branch_head, run_branch_head, f"origin/{RUN_BRANCH}")]
                )

    def test_stop_ctrl_c_and_no_terminal_leave_the_existing_branch_untouched(self):
        run_branch_head = self.existing_run_branch()
        for answer in (STOP, KeyboardInterrupt(), None):
            with self.subTest(answer=answer):
                confirm = None if answer is None else ScriptedConfirm(answer)

                result = self.start(loop_core.STANDARD_RUN, confirm=confirm)

                self.assert_rejected_at_startup(result, RUN_BRANCH)
                self.assertEqual(self.current_branch(), "main")
                self.assertEqual(self.repository.head(), self.base_head)
                self.assertEqual(git(self.repository.path, "rev-parse", RUN_BRANCH), run_branch_head)
                self.assertNotIn(RUN_BRANCH, self.origin_branches())
                if answer is None:
                    self.assertIn(f"switch to {RUN_BRANCH}", result.stderr)

    def test_failed_push_after_switch_stops_on_the_run_branch(self):
        run_branch_head = self.existing_run_branch()
        hook = self.repository.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'pushes are refused' >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

        result = self.start(loop_core.STANDARD_RUN, confirm=ScriptedConfirm(SWITCH))

        self.assert_rejected_at_startup(result, f"could not push {RUN_BRANCH}")
        self.assertEqual(self.current_branch(), RUN_BRANCH)
        self.assertEqual(self.repository.head(), run_branch_head)
        self.assertNotIn(RUN_BRANCH, self.origin_branches())

    def run_branch_on_origin_ahead(self, *, diverged=False):
        """origin's run branch is a commit ahead of the local one, which may diverge; returns both heads."""
        self.existing_run_branch(pushed=True)
        git(self.repository.path, "switch", "-q", RUN_BRANCH)
        behind = self.repository.head()
        self.commit_locally("published.txt")
        git(self.repository.path, "push", "-q", "origin", RUN_BRANCH)
        origin_head = self.repository.head()
        git(self.repository.path, "reset", "-q", "--hard", behind)
        if diverged:
            self.commit_locally("local-only.txt")
        git(self.repository.path, "switch", "-q", "main")
        return git(self.repository.path, "rev-parse", RUN_BRANCH), origin_head

    def test_switch_to_a_branch_only_on_origin_tracks_it_and_proceeds(self):
        origin_head = self.existing_run_branch(pushed=True)
        git(self.repository.path, "branch", "-D", RUN_BRANCH)
        at_first_session = []
        self.before_session = lambda request: at_first_session.append(
            (self.current_branch(), self.repository.head(), self.upstream())
        )
        confirm = ScriptedConfirm(SWITCH)

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"), confirm=confirm)

        self.assert_completed(result, sessions=1)
        self.assertEqual(confirm.questions, [f"Switch to the existing {RUN_BRANCH}?"])
        self.assertEqual(at_first_session, [(RUN_BRANCH, origin_head, f"origin/{RUN_BRANCH}")])
        self.assertEqual(git(self.repository.path, "rev-parse", "main"), self.base_head)
        self.assertEqual(self.repository.pushed_head(RUN_BRANCH), self.repository.head())

    def test_switch_fast_forwards_a_local_branch_behind_origin_before_any_session(self):
        _, origin_head = self.run_branch_on_origin_ahead()
        at_first_session = []
        self.before_session = lambda request: at_first_session.append(
            (self.current_branch(), self.repository.head(), self.upstream())
        )

        result = self.start(
            loop_core.STANDARD_RUN, ScriptedResult("committed"), confirm=ScriptedConfirm(SWITCH)
        )

        self.assert_completed(result, sessions=1)
        self.assertEqual(at_first_session, [(RUN_BRANCH, origin_head, f"origin/{RUN_BRANCH}")])
        self.assertEqual(git(self.repository.path, "rev-parse", "main"), self.base_head)
        self.assertEqual(len(self.repository.commits_since(origin_head)), 1)

    def test_switch_to_a_branch_diverged_from_origin_stops_on_it_with_both_sides_intact(self):
        local_head, origin_head = self.run_branch_on_origin_ahead(diverged=True)
        confirm = ScriptedConfirm(SWITCH)

        result = self.start(loop_core.STANDARD_RUN, confirm=confirm)

        self.assert_rejected_at_startup(result, f"{RUN_BRANCH} has diverged from origin/{RUN_BRANCH}")
        self.assertEqual(confirm.questions, [f"Switch to the existing {RUN_BRANCH}?"])
        self.assertEqual(self.current_branch(), RUN_BRANCH)
        self.assertEqual(self.repository.head(), local_head)
        self.assertEqual(self.repository.pushed_head(RUN_BRANCH), origin_head)
        self.assertEqual(git(self.repository.path, "rev-parse", "main"), self.base_head)

    def test_failed_fetch_stops_on_the_base_branch_with_nothing_changed(self):
        origin_head = self.existing_run_branch(pushed=True)
        git(self.repository.path, "branch", "-D", RUN_BRANCH)
        # A ref where the fetch must create a directory makes it fail to update the tracking ref.
        git(self.repository.path, "update-ref", "-d", f"refs/remotes/origin/{RUN_BRANCH}")
        git(self.repository.path, "update-ref", "refs/remotes/origin/macatia", "HEAD")

        result = self.start(loop_core.STANDARD_RUN, confirm=ScriptedConfirm(SWITCH))

        self.assert_rejected_at_startup(result, f"could not fetch {RUN_BRANCH} from origin")
        self.assertEqual(self.current_branch(), "main")
        self.assertEqual(self.repository.head(), self.base_head)
        self.assertNotIn(RUN_BRANCH, self.local_branches())
        self.assertEqual(self.repository.pushed_head(RUN_BRANCH), origin_head)

    def test_stop_leaves_a_branch_on_origin_untouched(self):
        for where in ("only on origin", "behind origin"):
            with self.subTest(where):
                self.new_run()
                self.github.add_issue(CHILD, parent=PARENT)
                git(self.repository.path, "checkout", "main")
                if where == "only on origin":
                    self.existing_run_branch(pushed=True)
                    git(self.repository.path, "branch", "-D", RUN_BRANCH)
                else:
                    self.run_branch_on_origin_ahead()
                local = {
                    ref: git(self.repository.path, "rev-parse", ref)
                    for ref in self.local_branches()
                }
                origin_head = self.repository.pushed_head(RUN_BRANCH)

                result = self.start(loop_core.STANDARD_RUN, confirm=ScriptedConfirm(STOP))

                self.assert_rejected_at_startup(result, RUN_BRANCH)
                self.assertEqual(self.current_branch(), "main")
                self.assertEqual(
                    {ref: git(self.repository.path, "rev-parse", ref) for ref in self.local_branches()},
                    local,
                )
                self.assertEqual(self.repository.pushed_head(RUN_BRANCH), origin_head)

    def test_a_non_base_branch_runs_with_no_question(self):
        git(self.repository.path, "checkout", BRANCH)
        confirm = ScriptedConfirm(STOP)

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"), confirm=confirm)

        self.assert_completed(result, sessions=1)
        self.assertEqual(confirm.questions, [])
        self.assertNotIn(RUN_BRANCH, self.origin_branches())


OUTER_PARENT = 190


class SubIssueRunTargetTests(BaseBranchTestCase):
    """A run started on a sub-issue is refused before its run branch is offered."""

    def make_sub_issue(self, *, parent_state="open", parent_labels=("work-started",)):
        self.github.add_issue(OUTER_PARENT, state=parent_state, labels=parent_labels)
        self.github.children[OUTER_PARENT] = [PARENT]

    def assert_refused_naming_the_parent(self, result, confirm, parent_labels=("work-started",)):
        self.assert_nothing_changed(
            result, f"issue #{PARENT} is a sub-issue of #{OUTER_PARENT}: run #{OUTER_PARENT} instead"
        )
        self.assertEqual(confirm.questions, [])
        self.assertEqual(self.github.comments(OUTER_PARENT), [])
        self.assertEqual(self.github.labels(OUTER_PARENT), set(parent_labels))
        self.assertEqual(self.github.labels(PARENT), {"ready-for-agent"})

    def test_sub_issue_with_sub_issues_is_refused_naming_its_parent(self):
        self.make_sub_issue()
        confirm = ScriptedConfirm(CREATE)

        result = self.start(loop_core.STANDARD_RUN, confirm=confirm)

        self.assert_refused_naming_the_parent(result, confirm)

    def test_sub_issue_without_sub_issues_is_refused_naming_its_parent(self):
        self.github.children[PARENT] = []
        self.make_sub_issue()
        confirm = ScriptedConfirm(CREATE)

        result = self.start(loop_core.STANDARD_RUN, confirm=confirm)

        self.assert_refused_naming_the_parent(result, confirm)

    def test_sub_issue_is_refused_whatever_the_state_of_its_parent(self):
        for parent_state, parent_labels in (
            ("closed", ()),
            ("open", ("human-review",)),
            ("open", ("needs-info",)),
        ):
            with self.subTest(parent_state=parent_state, parent_labels=parent_labels):
                self.make_sub_issue(parent_state=parent_state, parent_labels=parent_labels)
                confirm = ScriptedConfirm(CREATE)

                result = self.start(loop_core.STANDARD_RUN, confirm=confirm)

                self.assert_refused_naming_the_parent(result, confirm, parent_labels)

    def test_sub_issue_held_back_by_triage_is_refused_naming_its_parent(self):
        self.github.edit_issue_labels(PARENT, "needs-info", ["ready-for-agent"])
        self.make_sub_issue()
        confirm = ScriptedConfirm(CREATE)

        result = self.start(loop_core.STANDARD_RUN, confirm=confirm)

        self.assert_nothing_changed(
            result, f"issue #{PARENT} is a sub-issue of #{OUTER_PARENT}: run #{OUTER_PARENT} instead"
        )
        self.assertEqual(confirm.questions, [])

    def test_failed_parent_lookup_stops_the_run_as_an_error(self):
        self.github.parent_error[PARENT] = MacatiaError(
            f"command failed (gh api repos/lmpaul/buyr/issues/{PARENT}/parent): "
            "gh: Server Error (HTTP 502)"
        )
        confirm = ScriptedConfirm(CREATE)

        result = self.start(loop_core.STANDARD_RUN, confirm=confirm)

        self.assert_nothing_changed(result, "gh: Server Error (HTTP 502)")
        self.assertEqual(confirm.questions, [])
        self.assertEqual(self.github.labels(PARENT), {"ready-for-agent"})


class ParentTriageTests(StartupRejectionTestCase):
    """A parent that triage holds back is rejected at startup; children keep their own rules."""

    def start_on_parent(self, parent_labels, *results):
        self.new_run(parent_labels=parent_labels)
        self.github.add_issue(CHILD, parent=PARENT)
        return self.start(loop_core.STANDARD_RUN, *results)

    def assert_parent_rejected(self, result, parent_labels, blocking):
        self.assert_rejected_at_startup(
            result, f"parent issue #{PARENT} is held back by triage: {', '.join(blocking)}"
        )
        self.assertEqual(self.github.labels(PARENT), set(parent_labels))

    def test_parent_with_a_blocking_triage_label_is_rejected_at_startup(self):
        for label in ("needs-triage", "needs-info", "wontfix", "ready-for-human"):
            for parent_labels in ((label,), (label, "ready-for-agent")):
                with self.subTest(parent_labels=parent_labels):
                    result = self.start_on_parent(parent_labels)

                    self.assert_parent_rejected(result, parent_labels, [label])

    def test_error_names_every_blocking_triage_label(self):
        parent_labels = ("wontfix", "ready-for-agent", "needs-info")

        result = self.start_on_parent(parent_labels)

        self.assert_parent_rejected(result, parent_labels, ["needs-info", "wontfix"])

    def test_parent_without_a_blocking_triage_label_starts_normally(self):
        for parent_labels in ((), ("ready-for-agent",), ("work-started",)):
            with self.subTest(parent_labels=parent_labels):
                result = self.start_on_parent(parent_labels, ScriptedResult("committed"))

                self.assertEqual(result.exit_code, 0, result.stderr)
                self.assertEqual(self.session_children(), [CHILD])
                self.assertEqual(self.github.labels(CHILD), {"implemented"})
                self.assertIn("human-review", self.github.labels(PARENT))

    def test_renamed_blocking_triage_label_is_honoured_on_the_parent(self):
        self.repository.edit_configuration('needs_info = "needs-info"', 'needs_info = "r-needs-info"')
        self.github = FakeGitHub()
        self.github.add_issue(PARENT, labels=("r-needs-info",))
        self.github.add_issue(CHILD, parent=PARENT)

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_parent_rejected(result, ("r-needs-info",), ["r-needs-info"])


class ConfigurationRejectionTests(StartupRejectionTestCase):
    def test_verification_flags_that_are_not_toml_booleans_are_rejected_before_any_session(self):
        for key in ("local_checks", "ci_checks", "final_ci_check"):
            for value in ('"true"', '"false"', "1", "0"):
                with self.subTest(key=key, value=value):
                    self.repository = RunRepository(self, final_ci_check=True)
                    self.repository.edit_configuration(f"{key} = true", f"{key} = {value}")

                    result = self.start(loop_core.STANDARD_RUN)

                    self.assert_rejected_at_startup(
                        result, f"configuration.verification.{key} must be a boolean"
                    )

    def test_legacy_verification_keys_are_rejected_before_any_session(self):
        self.repository.edit_configuration(
            "local_checks = true\nci_checks = true", 'mode = "ci"\nlocal_gates = "enabled"'
        )

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_rejected_at_startup(result, "unknown keys: local_gates, mode")
        self.assertIn("upgrade with `uv tool upgrade macatia --reinstall`", result.stderr)

    def test_duplicate_toml_key_is_rejected_before_any_session(self):
        self.repository.edit_configuration("ci_checks = true\n", "ci_checks = true\nci_checks = true\n")

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_rejected_at_startup(result, "invalid TOML")

    def test_missing_agent_working_label_is_rejected_before_any_session(self):
        self.repository.edit_configuration('agent_working = "agent-working"\n', "")

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_rejected_at_startup(
            result, "configuration.labels is missing keys: agent_working"
        )

    def test_duplicate_label_value_is_rejected_before_any_session(self):
        for key, old, new in (
            ("agent_working", "agent-working", "implemented"),
            ("work_started", "work-started", "ready-for-agent"),
        ):
            with self.subTest(key=key):
                self.repository.edit_configuration(f'{key} = "{old}"', f'{key} = "{new}"')
                try:
                    result = self.start(loop_core.STANDARD_RUN)

                    self.assert_rejected_at_startup(result, "configuration.labels values must be unique")
                finally:
                    self.repository.edit_configuration(f'{key} = "{new}"', f'{key} = "{old}"')

    def test_unknown_engine_is_rejected_before_any_session(self):
        self.repository = RunRepository(self, engines={"implementation": "unknown"})

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_rejected_at_startup(
            result, "configuration.models.implementation.engine must be codex or claude"
        )

    def test_effort_claude_code_does_not_support_is_rejected_before_any_session(self):
        # Codex supports `minimal`; Claude Code does not.
        self.repository = RunRepository(self, engines={"implementation": "claude"})
        self.repository.edit_configuration(
            'name = "claude-implementation"\ndisplay_name = "Terra"\nreasoning_effort = "medium"',
            'name = "claude-implementation"\ndisplay_name = "Terra"\nreasoning_effort = "minimal"',
        )

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_rejected_at_startup(
            result, "configuration.models.implementation.reasoning_effort must be one of"
        )

    def remove_review_profile(self):
        configuration = (self.repository.path / "macatia.toml").read_text(encoding="utf-8")
        review_profile = re.search(r"\[models\.review\]\n.*?\n\n", configuration, flags=re.S).group(0)
        self.repository.edit_configuration(review_profile, "")

    def test_review_profile_is_optional_for_a_standard_run(self):
        self.remove_review_profile()

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_review_profile_is_required_for_a_reviewed_run(self):
        self.remove_review_profile()

        result = self.start(loop_core.REVIEWED_RUN, *passing_child_results(loop_core.REVIEWED_RUN))

        self.assert_rejected_at_startup(result, "configuration.models is missing keys: review")

    def add_backup(self, profile):
        self.repository.edit_configuration("\n[verification]", f"\n[models.backup]\n{profile}\n\n[verification]")

    def test_valid_backup_on_the_same_or_another_engine_is_accepted(self):
        for profile in (
            'engine = "codex"\nname = "gpt-5.6-luna"\ndisplay_name = "Luna"\nreasoning_effort = "medium"',
            # The regular profiles' model at another effort is not identical.
            'engine = "codex"\nname = "gpt-5.6-terra"\ndisplay_name = "Terra"\nreasoning_effort = "low"',
            'engine = "claude"\nname = "opus"\ndisplay_name = "Opus"\nreasoning_effort = "medium"',
        ):
            with self.subTest(profile=profile):
                self.setUp()
                self.add_backup(profile)

                result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

                self.assert_completed(result, sessions=1)
                self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_invalid_backup_is_rejected_before_any_session(self):
        for profile, reason in (
            (
                'engine = "codex"\nname = "gpt-5.6-luna"\nreasoning_effort = "medium"',
                "configuration.models.backup is missing keys: display_name",
            ),
            (
                'engine = "codex"\nname = "gpt-5.6-luna"\ndisplay_name = "Luna"\nreasoning_effort = "medium"\nroles = "all"',
                "configuration.models.backup has unknown keys: roles",
            ),
            (
                'engine = "unknown"\nname = "gpt-5.6-luna"\ndisplay_name = "Luna"\nreasoning_effort = "medium"',
                "configuration.models.backup.engine must be codex or claude",
            ),
            (
                'engine = "claude"\nname = "opus"\ndisplay_name = "Opus"\nreasoning_effort = "minimal"',
                "configuration.models.backup.reasoning_effort must be one of",
            ),
        ):
            with self.subTest(reason=reason):
                self.setUp()
                self.add_backup(profile)

                result = self.start(loop_core.STANDARD_RUN)

                self.assert_rejected_at_startup(result, reason)

    def test_backup_identical_to_a_regular_profile_is_rejected_before_any_session(self):
        for role, profile in (
            ("implementation", 'engine = "codex"\nname = "gpt-5.6-terra"\ndisplay_name = "Other name"\nreasoning_effort = "medium"'),
            # Omitting the engine means codex, as for the other profiles.
            ("implementation", 'name = "gpt-5.6-terra"\ndisplay_name = "Terra"\nreasoning_effort = "medium"'),
            ("review", 'engine = "codex"\nname = "gpt-5.6-sol"\ndisplay_name = "Sol"\nreasoning_effort = "high"'),
        ):
            with self.subTest(role=role, profile=profile):
                self.setUp()
                self.add_backup(profile)

                result = self.start(loop_core.STANDARD_RUN)

                self.assert_rejected_at_startup(
                    result,
                    f"configuration.models.backup must differ from configuration.models.{role} "
                    "in engine, model, or reasoning effort",
                )

    def test_backup_identical_to_the_correction_profile_is_rejected_before_any_session(self):
        self.repository.edit_configuration(
            '[models.correction]\nengine = "codex"\nname = "gpt-5.6-terra"',
            '[models.correction]\nengine = "codex"\nname = "gpt-5.6-luna"',
        )
        self.add_backup('engine = "codex"\nname = "gpt-5.6-luna"\ndisplay_name = "Luna"\nreasoning_effort = "medium"')

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_rejected_at_startup(
            result,
            "configuration.models.backup must differ from configuration.models.correction "
            "in engine, model, or reasoning effort",
        )


REVIEW_BY_DEFAULT_NOTICE = (
    "ℹ️ review_by_default is on in macatia.toml: this is a reviewed run, with a review "
    "session per child. Use --no-review for a standard run."
)


def set_review_by_default(repository, value="true"):
    repository.edit_configuration(
        'trusted_authors = ["lmpaul"]\n', f'trusted_authors = ["lmpaul"]\nreview_by_default = {value}\n'
    )


class ReviewByDefaultTests(StartupRejectionTestCase):
    """`review_by_default` picks the run kind when neither `--review` nor `--no-review` is given."""

    def start_with(self, flags, *results):
        """Start a run with the command-line `flags` before the parent."""
        self.sessions = ScriptedSessions(self.repository, *results)
        return start_run(self.repository, self.github, self.sessions, *flags, PARENT)

    def assert_reviewed_run_completed(self, result):
        self.assert_completed(result, sessions=2)
        self.assertIn("<!-- macatia-implementation-evidence: ", self.github.comments(CHILD)[-1])
        self.assertIn("<!-- macatia-parent-completion -->", self.github.comments(PARENT)[-2])

    def assert_standard_run_completed(self, result):
        self.assert_completed(result, sessions=1)
        self.assertIn("macatia-experimental-implementation-evidence", self.github.comments(CHILD)[-1])
        self.assertIn("<!-- macatia-experimental-parent-completion -->", self.github.comments(PARENT)[-2])

    def remove_review_profile(self):
        configuration = (self.repository.path / "macatia.toml").read_text(encoding="utf-8")
        review_profile = re.search(r"\[models\.review\]\n.*?\n\n", configuration, flags=re.S).group(0)
        self.repository.edit_configuration(review_profile, "")

    def test_setting_makes_a_run_without_flags_a_reviewed_run_and_says_so(self):
        set_review_by_default(self.repository)

        result = self.start_with((), *passing_child_results(loop_core.REVIEWED_RUN))

        self.assert_reviewed_run_completed(result)
        self.assertEqual(result.stdout.count(REVIEW_BY_DEFAULT_NOTICE), 1)
        self.assertLess(result.stdout.index(REVIEW_BY_DEFAULT_NOTICE), result.stdout.index("▶️"))
        self.assertIn("Review the exact", self.sessions.prompts[1])

    def test_setting_with_review_flag_runs_a_reviewed_run_without_the_notice(self):
        set_review_by_default(self.repository)

        result = self.start_with(("--review",), *passing_child_results(loop_core.REVIEWED_RUN))

        self.assert_reviewed_run_completed(result)
        self.assertNotIn("review_by_default", result.stdout + result.stderr)

    def test_no_review_flag_overrides_the_setting_without_needing_a_review_profile(self):
        set_review_by_default(self.repository)
        self.remove_review_profile()

        result = self.start_with(("--no-review",), *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_standard_run_completed(result)
        self.assertNotIn("review_by_default", result.stdout + result.stderr)

    def test_without_the_setting_no_review_flag_starts_a_standard_run(self):
        for value in (None, "false"):
            for flags in ((), ("--no-review",)):
                with self.subTest(value=value, flags=flags):
                    self.setUp()
                    if value is not None:
                        set_review_by_default(self.repository, value)

                    result = self.start_with(flags, *passing_child_results(loop_core.STANDARD_RUN))

                    self.assert_standard_run_completed(result)
                    self.assertNotIn("review_by_default", result.stdout + result.stderr)

    def test_review_and_no_review_together_are_a_usage_error(self):
        for flags in (("--review", "--no-review"), ("--no-review", "--review")):
            for setting in ("true", "false"):
                with self.subTest(flags=flags, setting=setting):
                    self.setUp()
                    set_review_by_default(self.repository, setting)
                    self.sessions = ScriptedSessions(self.repository)

                    result = start_run(
                        self.repository, self.github, self.sessions, flags[0], PARENT, flags[1]
                    )

                    self.assertEqual(result.exit_code, 2)
                    self.assert_rejected_at_startup(
                        result, "argument --no-review: not allowed with argument --review"
                    )

    def test_run_made_reviewed_by_the_setting_requires_the_review_profile(self):
        set_review_by_default(self.repository)
        self.remove_review_profile()

        result = self.start_with((), *passing_child_results(loop_core.REVIEWED_RUN))

        self.assert_rejected_at_startup(result, "configuration.models is missing keys: review")
        self.assertNotIn("review_by_default", result.stdout)

    def test_setting_that_is_not_a_toml_boolean_is_rejected_at_startup(self):
        for value in ('"true"', "1", "[]"):
            for flags in ((), ("--review",), ("--no-review",)):
                with self.subTest(value=value, flags=flags):
                    self.setUp()
                    set_review_by_default(self.repository, value)

                    result = self.start_with(flags)

                    self.assert_rejected_at_startup(
                        result, "configuration.review_by_default must be a boolean"
                    )


CAFFEINATE_BY_DEFAULT_NOTICE = (
    "ℹ️ caffeinate_by_default is on in macatia.toml: this run holds a sleep lock until it "
    "ends. Use --no-caffeinate to let the machine sleep."
)


def set_caffeinate_by_default(repository, value="true", path="macatia.toml"):
    repository.edit_configuration(
        'trusted_authors = ["lmpaul"]\n',
        f'trusted_authors = ["lmpaul"]\ncaffeinate_by_default = {value}\n',
        path,
    )


class SleepLockTests(StartupRejectionTestCase):
    """`--caffeinate` holds a sleep lock from run startup, once the configuration is loaded,
    until the run ends."""

    def start_with(self, *arguments, results):
        self.sessions = ScriptedSessions(self.repository, *results)
        return start_run(self.repository, self.github, self.sessions, *arguments)

    def test_flag_holds_the_sleep_lock_while_sessions_run_in_any_position(self):
        for arguments, mode in (
            (("--caffeinate", PARENT), loop_core.STANDARD_RUN),
            ((PARENT, "--caffeinate"), loop_core.STANDARD_RUN),
            (("--review", "--caffeinate", PARENT), loop_core.REVIEWED_RUN),
            ((PARENT, "--caffeinate", "--no-review"), loop_core.STANDARD_RUN),
        ):
            with self.subTest(arguments=arguments):
                self.setUp()

                results = passing_child_results(mode)

                result = self.start_with(*arguments, results=results)

                self.assert_completed(result, sessions=len(results))
                self.assertEqual(result.sleep_lock.taken, 1)
                self.assertEqual(result.sleep_lock.held_during_sessions, [True] * len(results))
                self.assertFalse(result.sleep_lock.held)

    def test_without_the_flag_no_sleep_lock_is_taken(self):
        for arguments in ((PARENT,), ("--review", PARENT), (PARENT, "--no-review")):
            with self.subTest(arguments=arguments):
                self.setUp()
                mode = loop_core.REVIEWED_RUN if "--review" in arguments else loop_core.STANDARD_RUN
                results = passing_child_results(mode)

                result = self.start_with(*arguments, results=results)

                self.assert_completed(result, sessions=len(results))
                self.assertEqual(result.sleep_lock.taken, 0)
                self.assertEqual(result.sleep_lock.held_during_sessions, [False] * len(results))

    def test_missing_or_invalid_configuration_stops_without_taking_the_sleep_lock(self):
        for name, break_configuration, reason in (
            ("missing", lambda: (self.repository.path / "macatia.toml").unlink(), "macatia.toml"),
            ("invalid", lambda: set_review_by_default(self.repository, '"yes"'),
             "configuration.review_by_default must be a boolean"),
        ):
            with self.subTest(name):
                self.setUp()
                break_configuration()

                result = self.start_with("--caffeinate", PARENT, results=())

                self.assert_rejected_at_startup(result, reason)
                self.assertEqual(result.sleep_lock.taken, 0)

    def test_setting_holds_the_sleep_lock_without_flags_and_says_so(self):
        set_caffeinate_by_default(self.repository)
        results = passing_child_results(loop_core.STANDARD_RUN)

        result = self.start_with(PARENT, results=results)

        self.assert_completed(result, sessions=len(results))
        self.assertEqual(result.sleep_lock.taken, 1)
        self.assertEqual(result.sleep_lock.held_during_sessions, [True] * len(results))
        self.assertEqual(result.stdout.count(CAFFEINATE_BY_DEFAULT_NOTICE), 1)
        self.assertLess(result.stdout.index(CAFFEINATE_BY_DEFAULT_NOTICE), result.stdout.index("▶️"))

    def test_setting_with_the_caffeinate_flag_holds_the_sleep_lock_without_the_notice(self):
        set_caffeinate_by_default(self.repository)
        results = passing_child_results(loop_core.STANDARD_RUN)

        result = self.start_with("--caffeinate", PARENT, results=results)

        self.assert_completed(result, sessions=len(results))
        self.assertEqual(result.sleep_lock.taken, 1)
        self.assertNotIn("caffeinate_by_default", result.stdout + result.stderr)

    def test_no_caffeinate_flag_overrides_the_setting_and_is_accepted_without_it(self):
        for value in ("true", "false", None):
            for arguments in (("--no-caffeinate", PARENT), (PARENT, "--no-caffeinate"),
                              ("--review", PARENT, "--no-caffeinate")):
                with self.subTest(value=value, arguments=arguments):
                    self.setUp()
                    if value is not None:
                        set_caffeinate_by_default(self.repository, value)
                    mode = loop_core.REVIEWED_RUN if "--review" in arguments else loop_core.STANDARD_RUN
                    results = passing_child_results(mode)

                    result = self.start_with(*arguments, results=results)

                    self.assert_completed(result, sessions=len(results))
                    self.assertEqual(result.sleep_lock.taken, 0)
                    self.assertNotIn("caffeinate_by_default", result.stdout + result.stderr)

    def test_setting_off_without_flags_takes_no_sleep_lock(self):
        set_caffeinate_by_default(self.repository, "false")
        results = passing_child_results(loop_core.STANDARD_RUN)

        result = self.start_with(PARENT, results=results)

        self.assert_completed(result, sessions=len(results))
        self.assertEqual(result.sleep_lock.taken, 0)
        self.assertNotIn("caffeinate_by_default", result.stdout + result.stderr)

    def test_caffeinate_and_no_caffeinate_together_are_a_usage_error(self):
        for arguments in (
            ("--caffeinate", "--no-caffeinate", PARENT),
            ("--no-caffeinate", PARENT, "--caffeinate"),
            (PARENT, "--caffeinate", "--review", "--no-caffeinate"),
        ):
            for setting in ("true", "false"):
                with self.subTest(arguments=arguments, setting=setting):
                    self.setUp()
                    set_caffeinate_by_default(self.repository, setting)

                    result = self.start_with(*arguments, results=())

                    self.assertEqual(result.exit_code, 2)
                    self.assert_rejected_at_startup(
                        result, "argument --no-caffeinate: not allowed with argument --caffeinate"
                    )
                    self.assertEqual(result.sleep_lock.taken, 0)

    def test_setting_that_is_not_a_toml_boolean_stops_without_taking_the_sleep_lock(self):
        for value in ('"true"', "1", "[]"):
            for flags in ((), ("--caffeinate",), ("--no-caffeinate",)):
                with self.subTest(value=value, flags=flags):
                    self.setUp()
                    set_caffeinate_by_default(self.repository, value)

                    result = self.start_with(*flags, PARENT, results=())

                    self.assert_rejected_at_startup(
                        result, "configuration.caffeinate_by_default must be a boolean"
                    )
                    self.assertEqual(result.sleep_lock.taken, 0)
                    self.assertNotIn(CAFFEINATE_BY_DEFAULT_NOTICE, result.stdout)

    def test_setting_in_the_configuration_given_by_config_holds_the_sleep_lock(self):
        (self.repository.path / "config").mkdir()
        git(self.repository.path, "mv", "macatia.toml", "config/macatia.toml")
        git(self.repository.path, "commit", "-m", "Move the Macatia configuration")
        set_caffeinate_by_default(self.repository, path="config/macatia.toml")
        results = passing_child_results(loop_core.STANDARD_RUN)

        result = self.start_with("--config", "config/macatia.toml", PARENT, results=results)

        self.assert_completed(result, sessions=len(results))
        self.assertEqual(result.sleep_lock.taken, 1)
        self.assertEqual(result.stdout.count(CAFFEINATE_BY_DEFAULT_NOTICE), 1)

    def test_sleep_lock_is_held_through_the_startup_checks(self):
        (self.repository.path / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")

        result = self.start_with("--caffeinate", PARENT, results=())

        self.assert_rejected_at_startup(result, "worktree is dirty at startup")
        self.assertEqual(result.sleep_lock.taken, 1)
        self.assertFalse(result.sleep_lock.held)

    def test_interrupted_run_releases_the_sleep_lock(self):
        result = self.start_with("--caffeinate", PARENT, results=(KeyboardInterrupt(),))

        self.assertEqual(result.exit_code, 130, result.stderr)
        self.assertIn("- final outcome: interrupted", self.github.comments(PARENT)[-1])
        self.assertEqual(result.sleep_lock.held_during_sessions, [True])
        self.assertFalse(result.sleep_lock.held)


class ConfiguredTrustTests(RunLevelTestCase):
    def test_repository_trusted_authors_remote_and_labels_come_from_the_configuration(self):
        labels = {
            "needs-triage": "r-needs-triage",
            "needs-info": "r-needs-info",
            "ready-for-agent": "r-ready-for-agent",
            "ready-for-human": "r-ready-for-human",
            "wontfix": "r-wontfix",
            "agent-working": "r-agent-working",
            "human-review": "r-human-review",
            "implemented": "r-implemented",
            "work-started": "r-work-started",
            "standalone": "r-standalone",
        }
        for default, configured in labels.items():
            self.repository.edit_configuration(f'= "{default}"\n', f'= "{configured}"\n')
        self.repository.edit_configuration('repository = "lmpaul/buyr"', 'repository = "acme/widget"')
        self.repository.edit_configuration('remote = "origin"', 'remote = "upstream"')
        self.repository.edit_configuration(
            f'trusted_authors = ["{TRUSTED_LOGIN}"]', 'trusted_authors = ["maintainer"]'
        )
        git(self.repository.path, "remote", "add", "upstream", str(self.repository.origin))
        git(self.repository.path, "remote", "remove", "origin")
        self.github = FakeGitHub(repository="acme/widget", login="maintainer")
        self.github.add_issue(PARENT, labels=("r-ready-for-agent",), author="maintainer")
        self.github.add_issue(
            CHILD,
            parent=PARENT,
            labels=("r-ready-for-agent",),
            author="maintainer",
            comments=[
                {"author": {"login": "maintainer"}, "body": "Owner requirement context."},
                # The default trusted author is not trusted by this configuration.
                {"author": {"login": TRUSTED_LOGIN}, "body": UNTRUSTED_COMMENT},
            ],
        )

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(self.github.opened_for, ["acme/widget"])
        self.assertEqual(self.github.labels(CHILD), {"r-implemented"})
        self.assertEqual(self.github.labels(PARENT), {"r-human-review"})
        self.assertEqual(git(self.repository.path, "config", f"branch.{BRANCH}.remote"), "upstream")
        self.assertEqual(self.repository.pushed_head(), self.repository.head())
        for prompt in self.sessions.prompts:
            self.assertNotIn(UNTRUSTED_COMMENT, prompt)

    def test_untrusted_issue_author_is_rejected_before_any_session(self):
        self.github.add_issue(CHILD, parent=PARENT, author="outside-contributor")

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_stopped_before_any_session(
            result, f"issue #{CHILD} must be authored by one of: @{TRUSTED_LOGIN}"
        )

    def test_untrusted_implementation_evidence_never_satisfies_a_blocker(self):
        for proof in ("ancestry", "merged pull request"):
            with self.subTest(proof):
                self.new_run()
                if proof == "ancestry":
                    verified_head = self.commit_prior_child()
                else:
                    verified_head = self.commit_off_the_branch()
                    self.github.commit_pulls[verified_head] = [CommitPull(43, self.commit_prior_child())]
                evidence = implementation_evidence(loop_core.STANDARD_RUN, verified_head)
                self.github.add_issue(
                    BLOCKER,
                    parent=PARENT,
                    labels=("implemented",),
                    comments=[evidence | {"author": {"login": "outside-contributor"}}],
                )
                self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[BLOCKER])

                result = self.start(loop_core.STANDARD_RUN)

                self.assert_stopped_before_any_session(
                    result, f"parent issue #{PARENT} has children remaining but none is runnable"
                )
                self.assertEqual(self.github.pull_lookups, [])


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
# The secrets the scripted local gate prints when it fails; Macatia must never repeat them.
GATE_SECRETS = ("super-secret", "hidden-token", "bearer-secret", "user:password", "```escaped fence")


def evidence_files(prompt, stream):
    return [pathlib.Path(path) for path in re.findall(rf"Full {stream}: (.+)", prompt)]


class VerificationTestCase(RunLevelTestCase):
    """One child under the parent; each test chooses its verification and CI."""

    def new_child_run(
        self, *, local_gate_results=None, local_checks=True, ci_checks=True, engines=None, **github
    ):
        self.repository = RunRepository(
            self,
            local_gate_results=local_gate_results,
            local_checks=local_checks,
            ci_checks=ci_checks,
            engines=engines,
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

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"), ScriptedResult("committed"))

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
            loop_core.STANDARD_RUN, ScriptedResult("committed", path=PERSISTENCE_PATH), ScriptedResult("committed")
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

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"), ScriptedResult("committed"))

        self.assert_implemented(result)
        prompt = self.sessions.prompts[1]
        self.assertIn("CI logs unavailable. Unable to download job logs.", prompt)
        self.assertIn("Job logs have expired", prompt)
        self.assertIn(f"Job URL: https://github.com/{self.github.repository}/actions/runs/777/job/1001", prompt)
        (evidence,) = self.repository.ci_evidence()
        self.assertFalse(evidence["logs_available"])
        self.assertEqual(evidence["log_error"], "Unable to download job logs.")

    def test_empty_job_log_is_never_used_as_evidence(self):
        self.new_child_run(ci=("failure", "success"), job_log_output="")

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"), ScriptedResult("committed"))

        self.assert_implemented(result)
        self.assertIn("CI logs unavailable. The downloaded job log is empty.", self.sessions.prompts[1])
        (evidence,) = self.repository.ci_evidence()
        self.assertFalse(evidence["logs_available"])
        self.assertEqual(evidence["log_error"], "The downloaded job log is empty.")

    def test_job_from_another_commit_is_never_used_as_evidence(self):
        self.new_child_run(ci=("failure", "success"), job_head_sha=OTHER_COMMIT)

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"), ScriptedResult("committed"))

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

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"), ScriptedResult("committed"))

        self.assert_implemented(result)
        prompt = self.sessions.prompts[1]
        self.assertIn("Job URL: unavailable", prompt)
        self.assertIn("CI logs unavailable. Actions job URL is unavailable", prompt)
        self.assertIn(annotation, prompt)
        self.assertEqual(self.github.job_log_requests, [])

    def test_stale_ci_evidence_never_authorizes_implemented(self):
        self.new_child_run(ci=({CANONICAL_CHECK: {"conclusion": "success", "head_sha": OTHER_COMMIT}},))

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

        self.assert_not_implemented(result, "infrastructure retries exhausted")
        self.assertIn(
            f"no required current-SHA check evidence is available for: {CANONICAL_CHECK}", result.stderr
        )
        self.assertEqual(self.github.labels(CHILD), {"human-review"})
        self.assertEqual(self.roles(), ["implementation"])

    def test_branch_advancing_after_ci_never_authorizes_implemented(self):
        self.github.ci = ["success"]

        def advance_the_branch(number, body):
            if number == CHILD and not is_child_cycle_start(body):
                self.github.after_comment = None
                self.commit_file("advance.txt", "Advance after CI")

        self.github.after_comment = advance_the_branch

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

        self.assert_not_implemented(result, "branch advanced after canonical CI")

    def test_branch_advancing_while_ci_is_pending_never_authorizes_implemented(self):
        self.new_child_run(ci=({CANONICAL_CHECK: {"status": "in_progress", "conclusion": None}}, "success"))

        def advance_the_branch(head_sha):
            self.github.before_check_runs = None
            self.commit_file("advance.txt", "Advance while CI is pending")

        self.github.before_check_runs = advance_the_branch

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

        self.assert_not_implemented(result, "branch advanced while waiting for canonical CI")
        self.assertEqual(len(self.github.checked_heads), 1)

    def test_no_changes_still_requires_ci_before_completion(self):
        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("no_changes"), ScriptedResult("committed"))

        self.assert_implemented(result)
        self.assertEqual(self.roles(), ["implementation", "CI correction"])
        (correction,) = self.repository.commits_since(self.repository.base_sha)
        self.assertEqual(self.github.checked_heads, [self.repository.base_sha, correction])

    def test_ci_waits_the_default_poll_interval_for_github_actions(self):
        self.new_child_run()

        with mock.patch.dict(os.environ):
            os.environ.pop("MACATIA_CI_POLL_SECONDS", None)
            result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

        self.assert_implemented(result)
        self.assertEqual(result.sleeps, [45.0])
        self.assertIn("Waiting 45 seconds for GitHub Actions to start.", result.stdout)

    def test_pending_ci_reports_progress_every_five_minutes_without_a_terminal(self):
        pending = {"status": "in_progress", "conclusion": None}
        self.new_child_run(
            ci=({CANONICAL_CHECK: "success", DATABASE_CHECK: pending},) * 15 + ("success",),
            check_names=(CANONICAL_CHECK, DATABASE_CHECK),
        )

        with mock.patch.dict(os.environ):
            os.environ.pop("MACATIA_CI_POLL_SECONDS", None)
            result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed", path=PERSISTENCE_PATH))

        self.assert_implemented(result)
        head = self.github.checked_heads[-1][:7]
        progress = [line for line in result.stdout.splitlines() if "still running" in line]
        self.assertEqual(progress, [
            f"⏳ Canonical CI still running for {head} after {elapsed}: "
            f"1/2 required checks done, waiting on {DATABASE_CHECK}."
            for elapsed in ("5 min 15 s", "10 min 30 s")
        ])
        self.assertNotIn("\r", result.stdout)


class TerminalStream(io.StringIO):
    def isatty(self):
        return True


class CIProgressTests(unittest.TestCase):
    def progress(self, *, columns=200):
        self.now = 100.0
        self.stream = TerminalStream()
        size = os.terminal_size((columns, 24))
        self.enterContext(mock.patch.object(loop_core.shutil, "get_terminal_size", return_value=size))
        return loop_core.CIProgress("a1b2c3d4e5", clock=lambda: self.now, stream=self.stream)

    def test_a_terminal_rewrites_one_line_in_place_and_clears_it_at_the_end(self):
        progress = self.progress()

        progress.update("0/1 required checks done, waiting on Canonical CI")
        self.now += 270
        progress.update("0/1 required checks done, waiting on Canonical CI")
        progress.close()

        self.assertEqual(self.stream.getvalue(), (
            "\r\033[K⏳ Canonical CI still running for a1b2c3d after 0 s: "
            "0/1 required checks done, waiting on Canonical CI."
            "\r\033[K⏳ Canonical CI still running for a1b2c3d after 4 min 30 s: "
            "0/1 required checks done, waiting on Canonical CI."
            "\r\033[K"
        ))

    def test_a_terminal_line_never_wraps(self):
        progress = self.progress(columns=30)

        progress.update("0/1 required checks done, waiting on Canonical CI")

        self.assertEqual(self.stream.getvalue(), "\r\033[K⏳ Canonical CI still running")

    def test_closing_without_an_update_writes_nothing(self):
        self.progress().close()

        self.assertEqual(self.stream.getvalue(), "")


class ConditionalCITests(VerificationTestCase):
    def setUp(self):
        self.new_child_run(check_names=(CANONICAL_CHECK, DATABASE_CHECK))

    def test_persistence_change_completes_with_a_green_database_check_for_its_head(self):
        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed", path=PERSISTENCE_PATH))

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
                    loop_core.STANDARD_RUN, ScriptedResult("committed", path=PERSISTENCE_PATH), ScriptedResult("committed")
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

                result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed", path=PERSISTENCE_PATH))

                self.assert_not_implemented(result, "infrastructure retries exhausted")
                self.assertIn(reason, result.stderr)
                self.assertEqual(self.roles(), ["implementation"])

    def test_frontend_only_change_does_not_require_the_database_check(self):
        self.github.ci = [{CANONICAL_CHECK: "success", DATABASE_CHECK: "failure"}]

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed", path=FRONTEND_PATH))

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

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"), ScriptedResult("committed"))

        evidence = self.assert_implemented(result)
        self.assertEqual(self.roles(), ["implementation", "CI correction"])
        self.assertIn("Local quality gates are disabled by Macatia configuration.", result.stdout)
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


SECOND_CHILD = 194
THIRD_CHILD = 195
FINAL_CI_GREEN = "- final CI check: CI is green for exact final head `{head}`\n"


class FinalCICheckTests(VerificationTestCase):
    """`final_ci_check = true` with `ci_checks = false`: one final CI check on the run's final head."""

    def new_final_ci_run(self, *, children=(CHILD, SECOND_CHILD), local_checks=True, **github):
        self.repository = RunRepository(
            self, local_checks=local_checks, ci_checks=False, final_ci_check=True
        )
        self.github = FakeGitHub(**github)
        self.github.add_issue(PARENT)
        for child in children:
            self.github.add_issue(child, parent=PARENT)

    def setUp(self):
        self.new_final_ci_run()

    def observe_pull_request_drafts(self):
        """Record whether the cumulative draft PR is a draft as each agent session starts."""
        self.observed_drafts = []

        def observe(request):
            pull = self.github.pull_request()
            self.observed_drafts.append(None if pull is None else pull.is_draft)

        self.before_session = observe

    def assert_final_ci_stopped(self, result, reason):
        """The run target went to human review; children stay implemented and the PR stays draft."""
        self.assertEqual(result.exit_code, 1)
        self.assertIn(reason, result.stderr)
        for child in (CHILD, SECOND_CHILD):
            self.assertEqual(self.github.labels(child), {"implemented"})
        self.assertEqual(self.github.labels(PARENT), {"human-review"})
        self.assertTrue(self.github.pull_request().is_draft)
        parent_comments = self.github.comments(PARENT)
        self.assertFalse(any("parent-completion" in body for body in parent_comments))
        self.assertIn("- final outcome: stopped safely", parent_comments[-1])
        self.assertIn("- final outcome: stopped safely", self.github.pull_request().comments[-1])
        self.assertNotIn("- final CI check:", parent_comments[-1])
        handoff = parent_comments[-2]
        self.assertTrue(handoff.startswith(f"{HANDOFF_MARKER}\nMacatia stopped"), handoff)
        self.assertIn("children stay implemented", handoff)
        return handoff

    def test_children_publish_without_a_ci_wait_and_the_final_head_gets_one_ci_check(self):
        for mode in MODES:
            with self.subTest(mode.diagnostics_label):
                self.new_final_ci_run()
                self.observe_pull_request_drafts()

                result = self.start(mode, *passing_child_results(mode) * 2)

                self.assert_completed(result, sessions=len(passing_child_results(mode)) * 2)
                head = self.repository.head()
                self.assertEqual(self.github.checked_heads, [head])
                # The cumulative draft PR exists and stays a draft between children.
                self.assertEqual(self.observed_drafts[0], None)
                self.assertEqual(self.observed_drafts[-1], True)
                self.assertEqual(self.repository.pushed_head(), head)
                for child in (CHILD, SECOND_CHILD):
                    self.assertEqual(self.github.labels(child), {"implemented"})
                    evidence = self.github.comments(child)[-1]
                    self.assertIn("; CI is verified at the end of the run.", evidence)
                    self.assertNotIn("remote CI was not required", evidence)
                    self.assertNotIn("CI are green", evidence)
                local = (
                    "Applicable local gates and independent agent review completed for exact head"
                    if mode.review else "Applicable local gates are green for exact head"
                )
                self.assertIn(local, self.github.comments(CHILD)[-1])
                summary = FINAL_CI_GREEN.format(head=head)
                self.assertIn(summary, result.stdout)
                self.assertIn(summary, self.github.comments(PARENT)[-1])
                self.assertIn("- final outcome: completed", self.github.comments(PARENT)[-1])
                self.assertIn("parent-completion", self.github.comments(PARENT)[-2])
                pull = self.github.pull_request()
                self.assertIn(summary, pull.comments[-1])
                self.assertFalse(pull.is_draft)
                self.assertIn("Checking canonical CI (attempt 1/4).", result.stdout)
                timings = json.loads((self.repository.diagnostics() / "timings.json").read_text())
                self.assertIn(
                    {"phase": "final_ci_wait", "label": "GitHub Actions wait", "issue_number": PARENT},
                    [{key: phase[key] for key in ("phase", "label", "issue_number")}
                     for phase in timings["phases"] if "issue_number" in phase],
                )

    def test_evidence_without_local_gates_says_no_automated_check_ran_for_the_child(self):
        for mode in MODES:
            with self.subTest(mode.diagnostics_label):
                self.new_final_ci_run(children=(CHILD,), local_checks=False)

                result = self.start(mode, *passing_child_results(mode))

                self.assert_completed(result, sessions=len(passing_child_results(mode)))
                head = self.repository.head()
                self.assertEqual(self.repository.local_gate_heads(), [])
                self.assertEqual(self.github.checked_heads, [head])
                self.assertNotIn(mode.unverified_warning, result.stdout)
                done = "Independent agent review" if mode.review else "Implementation"
                self.assertIn(
                    f"{done} completed for exact head `{head}`; no automated check ran for this "
                    "child and CI is verified at the end of the run.",
                    self.github.comments(CHILD)[-1],
                )

    def test_ci_checks_on_makes_final_ci_check_ineffective(self):
        self.repository = RunRepository(self, ci_checks=True, final_ci_check=True)
        self.github = FakeGitHub()
        self.github.add_issue(PARENT)
        self.github.add_issue(CHILD, parent=PARENT)

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.github.checked_heads, [self.repository.head()])
        self.assertNotIn("- final CI check:", result.stdout)
        self.assertIn("Applicable local gates and canonical CI are green", self.github.comments(CHILD)[-1])

    def test_final_ci_check_off_behaves_as_without_the_key(self):
        for final_ci_check in (None, False):
            with self.subTest(final_ci_check=final_ci_check):
                self.repository = RunRepository(self, ci_checks=False, final_ci_check=final_ci_check)
                self.github = FakeGitHub()
                self.github.add_issue(PARENT)
                self.github.add_issue(CHILD, parent=PARENT)

                result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

                self.assert_completed(result, sessions=1)
                self.assertEqual(self.github.checked_heads, [])
                self.assertNotIn("- final CI check:", result.stdout)
                self.assertIn("remote CI was not required", self.github.comments(CHILD)[-1])
                self.assertFalse(self.github.pull_request().is_draft)

    def test_required_checks_follow_every_path_changed_since_the_first_child_base(self):
        self.new_final_ci_run(
            ci=({CANONICAL_CHECK: "success", DATABASE_CHECK: "failure"},),
            check_names=(CANONICAL_CHECK, DATABASE_CHECK),
        )

        result = self.start(
            loop_core.STANDARD_RUN,
            ScriptedResult("committed", path=PERSISTENCE_PATH),
            ScriptedResult("committed", path=FRONTEND_PATH),
        )

        handoff = self.assert_final_ci_stopped(result, "final CI check failed")
        self.assertIn(f"{DATABASE_CHECK} concluded failure", handoff)

    def test_red_final_ci_hands_the_parent_to_human_review_with_the_diagnostic(self):
        self.new_final_ci_run(ci=("failure",), job_log_output=CI_LOG)

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"), ScriptedResult("committed"))

        handoff = self.assert_final_ci_stopped(result, "final CI check failed")
        head = self.repository.head()
        self.assertEqual(self.roles(), ["implementation", "implementation"])
        self.assertEqual(self.github.checked_heads, [head])
        self.assertIn(f"exact final head `{head}`", handoff)
        self.assertIn("Last diagnostic:", handoff)
        self.assertIn(f"{CANONICAL_CHECK} concluded failure", handoff)
        self.assertIn("[REDACTED]", handoff)
        self.assertNotIn("ci-secret", handoff)
        self.assertIn(f"#{PARENT} was handed to human review because its final CI check failed", result.stderr)
        self.assertNotIn("🎉", result.stdout)
        self.assertEqual(
            [evidence["issue_number"] for evidence in self.repository.ci_evidence()], [PARENT]
        )

    def test_unavailable_final_ci_hands_the_parent_to_human_review(self):
        pending = {"status": "in_progress", "conclusion": None}
        for name, ci, environment, reason in (
            ("retries", ("cancelled",), {}, "infrastructure retries exhausted for canonical CI"),
            ("timeout", ({CANONICAL_CHECK: pending},), {"MACATIA_CI_TIMEOUT_SECONDS": "60"},
             "canonical CI did not become green"),
        ):
            with self.subTest(name):
                self.new_final_ci_run(ci=ci)

                with mock.patch.dict(os.environ, environment):
                    result = self.start(
                        loop_core.STANDARD_RUN, ScriptedResult("committed"), ScriptedResult("committed")
                    )

                handoff = self.assert_final_ci_stopped(result, reason)
                self.assertIn("CI evidence could not be obtained", handoff)
                self.assertIn(reason, handoff)
                self.assertEqual(self.roles(), ["implementation", "implementation"])
                self.assertIn(
                    f"#{PARENT} was handed to human review because CI evidence could not be "
                    "obtained for its final CI check",
                    result.stderr,
                )

    def test_stopped_or_interrupted_run_makes_no_final_ci_check(self):
        for name, second in (
            ("human review", ScriptedResult("blocked")),
            ("interrupted", KeyboardInterrupt()),
        ):
            with self.subTest(name):
                self.new_final_ci_run()

                result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"), second)

                self.assertNotEqual(result.exit_code, 0)
                self.assertEqual(self.github.checked_heads, [])
                self.assertEqual(self.github.labels(CHILD), {"implemented"})
                self.assertTrue(self.github.pull_request().is_draft)
                self.assertNotIn("- final CI check:", self.github.comments(PARENT)[-1])

    def test_dependency_deadlock_makes_no_final_ci_check(self):
        self.new_final_ci_run(children=(CHILD,))
        self.github.add_issue(SECOND_CHILD, parent=PARENT, blocked_by=[THIRD_CHILD])
        self.github.add_issue(THIRD_CHILD, parent=PARENT, blocked_by=[SECOND_CHILD])

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

        self.assertEqual(result.exit_code, 1)
        self.assertIn("dependency deadlock", result.stderr)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        self.assertEqual(self.github.checked_heads, [])
        self.assertTrue(self.github.pull_request().is_draft)

    def test_run_with_every_child_already_implemented_checks_the_published_head(self):
        self.new_final_ci_run(children=())
        self.github.add_issue(CHILD, parent=PARENT, labels=("implemented",))
        head = self.commit_prior_child()
        git(self.repository.path, "push", "-u", "origin", BRANCH)

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_completed(result, sessions=0)
        self.assertEqual(self.github.checked_heads, [head])
        self.assertIn(FINAL_CI_GREEN.format(head=head), self.github.comments(PARENT)[-1])
        self.assertFalse(self.github.pull_request().is_draft)


class HandoffRedactionTests(VerificationTestCase):
    def test_handoff_comments_keep_the_last_diagnostic_redacted(self):
        corrections = [ScriptedResult("committed")] * 4
        for name, run_options, shown, secrets in (
            ("local gate", {"local_gate_results": ("fail",)}, "fixture gate failed:", GATE_SECRETS),
            ("CI", {"ci": ("failure",), "job_log_output": CI_LOG}, "token=[REDACTED]", ("ci-secret",)),
        ):
            with self.subTest(name):
                self.new_child_run(**run_options)

                result = self.start(loop_core.STANDARD_RUN, *corrections)

                self.assert_not_implemented(result, "Stop reason: correction limit of 3 exhausted")
                handoff = self.github.comments(CHILD)[-1]
                self.assertIn("Last diagnostic:", handoff)
                self.assertIn(shown, handoff)
                self.assertIn("[REDACTED]", handoff)
                for secret in secrets:
                    self.assertNotIn(secret, handoff)


class CrashedRunTests(VerificationTestCase):
    def test_crash_publishes_a_crashed_terminal_summary_and_is_not_swallowed(self):
        crash = ValueError("the session adapter broke")

        with self.assertRaises(ValueError) as raised:
            self.start(loop_core.STANDARD_RUN, crash)

        self.assertIs(raised.exception, crash)
        self.assertIn("- final outcome: crashed", self.github.comments(PARENT)[-1])
        self.assertEqual(self.github.labels(CHILD), {"agent-working"})
        self.assertEqual(self.github.reports(CHILD), [])


class InterruptedRunTests(VerificationTestCase):
    """Ctrl-C, SIGTERM, and SIGHUP end a run with an interrupted terminal summary."""

    def assert_interrupted(self, result, exit_code, signal_name, *, rerun_action=True):
        """The interrupted summary was published and printed; `rerun_action` says
        whether the run's own next action is to rerun, which a handed-off child's
        action replaces."""
        self.assertEqual(result.exit_code, exit_code, result.stderr)
        summary = self.github.comments(PARENT)[-1]
        rerun = f"Macatia was interrupted: run `macatia run {PARENT}` again to continue."
        for published in (summary, result.stderr):
            self.assertIn("- final outcome: interrupted", published)
            self.assertIn(f"Stop reason: interrupted by {signal_name}", published)
            if rerun_action:
                self.assertIn(rerun, published)
            else:
                self.assertNotIn(rerun, published)

    def assert_handed_to_human_review_unverified(self, result, base, head):
        """The child went to human review naming the commits nobody verified."""
        unverified = f"{base}..{head}"
        self.assertEqual(self.github.labels(CHILD), {"human-review"})
        self.assertEqual(len(self.github.reports(CHILD)), 1)
        handoff = self.github.reports(CHILD)[0]
        self.assertTrue(handoff.startswith(
            f"{HANDOFF_MARKER}\nMacatia stopped because the run was interrupted"
        ), handoff)
        self.assertIn(unverified, handoff)
        self.assertIn(COMMITS_STAY, handoff)
        summary = self.github.comments(PARENT)[-1]
        for published in (summary, result.stderr):
            self.assertIn(
                f"- #{CHILD}: interrupted and handed to human review with the unverified "
                f"commits {unverified} ({self.github.latest_comment_url(CHILD)}).",
                published,
            )
            self.assertIn(
                f"- #{CHILD} was interrupted: inspect the unverified commits {unverified}, "
                f"then close it or relabel it ready-for-agent and run "
                f"`macatia run {PARENT}` again.",
                published,
            )

    def test_ctrl_c_before_the_first_commit_returns_the_child_to_ready_for_agent(self):
        result = self.start(loop_core.STANDARD_RUN, KeyboardInterrupt())

        self.assert_interrupted(result, 130, "SIGINT")
        self.assertEqual(self.roles(), ["implementation"])
        self.assertIsNone(self.github.pull_request())
        self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})
        self.assertEqual(self.github.reports(CHILD), [])
        self.assertIn(f"- #{CHILD}: runnable.", self.github.comments(PARENT)[-1])

    def test_a_terminal_sees_the_cleanup_after_ctrl_c(self):
        result = self.start(loop_core.STANDARD_RUN, KeyboardInterrupt(), terminal=True)

        self.assert_interrupted(result, 130, "SIGINT")
        self.assertIn(
            f"Interrupted: releasing #{CHILD} and publishing the summary… "
            "(Ctrl-C again skips the cleanup)",
            " ".join(result.stdout.split()),
        )

    def test_other_output_gets_no_cleanup_spinner(self):
        result = self.start(loop_core.STANDARD_RUN, KeyboardInterrupt())

        self.assertNotIn("Ctrl-C again", result.stdout + result.stderr)

    def interrupt_while_waiting_for_ci(self, signal_number):
        """Interrupt the run once the child's commit waits for CI, as Ctrl-C or `kill` would."""

        def interrupt(head):
            os.kill(os.getpid(), signal_number)

        self.github.before_check_runs = interrupt

    def test_ctrl_c_after_a_commit_hands_the_child_to_human_review(self):
        base = self.repository.head()
        self.interrupt_while_waiting_for_ci(signal.SIGINT)

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

        self.assert_interrupted(result, 130, "SIGINT", rerun_action=False)
        self.assertEqual(self.roles(), ["implementation"])
        self.assert_handed_to_human_review_unverified(result, base, self.repository.head())

    def test_sigterm_and_sighup_during_an_agent_session_behave_like_ctrl_c(self):
        for signal_number, exit_code in ((signal.SIGTERM, 143), (signal.SIGHUP, 129)):
            with self.subTest(signal_number.name, commit=False):
                self.new_child_run()
                handler = signal.getsignal(signal_number)

                result = self.start(loop_core.STANDARD_RUN, ScriptedSignal(signal_number))

                self.assert_interrupted(result, exit_code, signal_number.name)
                self.assertEqual(self.roles(), ["implementation"])
                self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})
                self.assertEqual(self.github.reports(CHILD), [])
                self.assertIs(signal.getsignal(signal_number), handler)
            with self.subTest(signal_number.name, commit=True):
                self.new_child_run()
                base = self.repository.head()
                self.interrupt_while_waiting_for_ci(signal_number)

                result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

                self.assert_interrupted(
                    result, exit_code, signal_number.name, rerun_action=False
                )
                self.assert_handed_to_human_review_unverified(result, base, self.repository.head())

    def test_a_signal_ignored_at_launch_stays_ignored(self):
        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGHUP, previous)
        self.sessions = ScriptedSessions(self.repository, ScriptedResult("committed"))
        self.sessions.before_session = lambda request: os.kill(os.getpid(), signal.SIGHUP)

        result = start_mode_run(loop_core.STANDARD_RUN, self.repository, self.github, self.sessions, PARENT)

        self.assert_implemented(result)
        self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)

    def test_ctrl_c_after_the_evidence_is_published_marks_the_child_implemented(self):
        def interrupt_after_evidence(number, body):
            if number == CHILD and not is_child_cycle_start(body):
                raise KeyboardInterrupt

        self.github.after_comment = interrupt_after_evidence

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

        self.assertEqual(result.exit_code, 130, result.stderr)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        self.assertEqual(len(self.github.reports(CHILD)), 1)
        self.assertIn("- final outcome: interrupted", self.github.comments(PARENT)[-1])

    def test_interruption_between_two_children_publishes_the_same_summary(self):
        self.github.add_issue(DEPENDENT, parent=PARENT)

        def interrupt_once_implemented(number, add, remove):
            if number == CHILD and add == "implemented":
                raise KeyboardInterrupt

        self.github.after_label_edit = interrupt_once_implemented

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

        self.assert_interrupted(result, 130, "SIGINT")
        self.assertEqual(self.session_children(), [CHILD])
        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        self.assertEqual(self.github.labels(DEPENDENT), {"ready-for-agent"})
        self.assertIn("- final outcome: interrupted", self.github.pull_request().comments[-1])

    def test_second_interruption_during_the_cleanup_exits_immediately(self):
        def interrupt_again(number, body):
            if number == PARENT:
                raise KeyboardInterrupt

        self.github.after_comment = interrupt_again

        result = self.start(loop_core.STANDARD_RUN, KeyboardInterrupt())

        self.assertEqual(result.exit_code, 130)
        self.assertIn("- final outcome: interrupted", self.github.comments(PARENT)[-1])
        self.assertIn(
            "Macatia was interrupted again; exiting without finishing the cleanup.", result.stderr
        )
        self.assertNotIn("Macatia summary", result.stderr)

    def test_second_interruption_during_the_release_leaves_the_child_agent_working(self):
        def interrupt_again(number, body):
            if number == CHILD and not is_child_cycle_start(body):
                raise KeyboardInterrupt

        self.github.after_comment = interrupt_again
        self.interrupt_while_waiting_for_ci(signal.SIGINT)

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

        self.assertEqual(result.exit_code, 130)
        self.assertEqual(self.github.labels(CHILD), {"agent-working"})
        self.assertEqual(self.github.comments(PARENT), [])
        self.assertIn(
            "Macatia was interrupted again; exiting without finishing the cleanup.", result.stderr
        )

    def test_simulated_process_death_is_neither_caught_nor_reported(self):
        with self.assertRaises(RunInterrupted):
            self.start(loop_core.STANDARD_RUN, RunInterrupted("killed with SIGKILL"))

        self.assertEqual(self.github.comments(PARENT), [])
        self.assertEqual(self.github.labels(CHILD), {"agent-working"})


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
        self.assertIn("- final outcome: stopped safely", self.github.comments(PARENT)[-1])
        self.assertIn("- final outcome: stopped safely", self.github.pull_request().comments[-1])

    def test_unavailable_cancelled_and_timed_out_ci_is_retried_without_another_session(self):
        for conclusion in ("unavailable", "cancelled", "timed_out"):
            with self.subTest(conclusion):
                self.new_child_run(ci=(conclusion,))

                with mock.patch.dict(os.environ, {"MACATIA_INFRA_RETRY_SECONDS": "30"}):
                    result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

                self.assert_stopped_after_infrastructure_retries(
                    result, f"{CANONICAL_CHECK} concluded {conclusion}"
                )
                self.assertEqual(result.sleeps[-3:], [30.0] * 3)
                self.assertIn("Checking canonical CI (attempt 4/4).", result.stdout)

    def test_authentication_failures_and_service_outages_are_infrastructure_failures(self):
        for failure in ("HTTP 401: Bad credentials", "HTTP 503: Service Unavailable"):
            with self.subTest(failure):
                self.new_child_run(ci=(MacatiaError(f"gh api failed: {failure}"),))

                result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

                self.assert_stopped_after_infrastructure_retries(result, f"gh api failed: {failure}")

    def test_infrastructure_retry_that_succeeds_does_not_count_toward_the_child_session_limit(self):
        for mode in MODES:
            with self.subTest(mode.diagnostics_label):
                self.new_child_run(ci=("cancelled", "success"))
                sessions = passing_child_results(mode)

                set_child_session_limit(
                    self.repository, **{"reviewed" if mode.review else "standard": len(sessions)}
                )

                result = self.start(mode, *sessions)

                self.assert_implemented(result)
                self.assertIn(
                    f"- agent sessions: {len(sessions)}\n- infrastructure retries: 1\n", result.stdout
                )
                self.assertIn("Canonical CI is not ready; the next check is in", result.stdout)
                self.assertIn("Checking canonical CI (attempt 2/4).", result.stdout)
                self.assertEqual(self.github.checked_heads, [self.repository.head()] * 2)

    def test_branch_advancing_during_an_infrastructure_retry_stops_without_another_check(self):
        self.new_child_run(ci=("cancelled", "success"))

        def advance_the_branch(head_sha):
            self.github.before_check_runs = None
            self.commit_file("advance.txt", "Advance during the retry")

        self.github.before_check_runs = advance_the_branch

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

        self.assert_not_implemented(result, "branch advanced during an infrastructure retry")
        self.assertEqual(len(self.github.checked_heads), 1)
        self.assertEqual(self.roles(), ["implementation"])
        self.assertIn("- infrastructure retries: 0\n", self.github.comments(PARENT)[-1])


CLOSED_CHILD = 195
IMPLEMENTED_CHILD = 196
HUMAN_REVIEW_CHILD = 197
READY_FOR_HUMAN_CHILD = 198


class ChildSessionLimitTests(VerificationTestCase):
    def new_two_child_run(self):
        self.new_run()
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.add_issue(DEPENDENT, parent=PARENT)

    def assert_child_session_limit_stop(self, result, child, *, role, limit, mode, sessions):
        """The run stopped on `child`'s session limit and handed it to human review."""
        self.assertEqual(result.exit_code, 1)
        self.assertIn(f"Stop reason: child session limit exhausted before {role}", result.stderr)
        self.assertEqual(len(self.sessions.requests), sessions)
        self.assertEqual(self.github.labels(child), {"human-review"})
        handoff = self.github.comments(child)[-1]
        self.assertTrue(handoff.startswith(
            f"{HANDOFF_MARKER}\nMacatia stopped before {role} because the child session limit of {limit} "
            f"({mode} run) was exhausted. This child remains open for human review."
        ), handoff)
        self.assertIn(COMMITS_STAY, handoff)
        summary = self.github.comments(PARENT)[-1]
        self.assertIn(f"- agent sessions: {sessions}\n", summary)
        self.assertIn("- final outcome: stopped safely", summary)
        self.assertIn(f"- agent sessions: {sessions}\n", result.stderr)
        self.assertEqual(self.github.labels(PARENT), {"work-started"})
        return handoff

    def test_each_child_receives_its_worst_case_whatever_the_number_of_children(self):
        for mode in MODES:
            with self.subTest(mode.diagnostics_label):
                self.new_two_child_run()
                self.ready_for_agent_during_the_run(READY_FOR_HUMAN_CHILD)
                worst_case = worst_case_child_results(mode)

                result = self.start(mode, *worst_case * 3)

                self.assert_completed(result, sessions=len(worst_case) * 3)
                self.assertEqual(
                    self.session_children(),
                    [CHILD] * len(worst_case) + [DEPENDENT] * len(worst_case)
                    + [READY_FOR_HUMAN_CHILD] * len(worst_case),
                )
                self.assertIn(
                    f"This run used {len(worst_case) * 3} agent sessions;",
                    self.github.comments(PARENT)[-2],
                )

    def test_limit_below_the_worst_case_stops_the_child_and_the_run(self):
        self.new_two_child_run()
        findings = ScriptedResult("needs_correction", findings=("Handle empty carts.",), commit=True)

        set_child_session_limit(self.repository, standard=3)

        result = self.start(loop_core.STANDARD_RUN, *[findings] * 3)

        handoff = self.assert_child_session_limit_stop(
            result, CHILD, role="issue correction", limit=3, mode="standard", sessions=3
        )
        self.assertIn("Last diagnostic:\n```text\n- Handle empty carts.\n```", handoff)
        self.assertEqual(self.session_children(), [CHILD] * 3)
        self.assertEqual(self.github.labels(DEPENDENT), {"ready-for-agent"})

    def test_handed_off_child_relabelled_and_rerun_gets_a_fresh_count(self):
        findings = ScriptedResult("needs_correction", findings=("Handle empty carts.",), commit=True)
        set_child_session_limit(self.repository, standard=1)
        result = self.start(loop_core.STANDARD_RUN, findings)
        self.assert_child_session_limit_stop(
            result, CHILD, role="issue correction", limit=1, mode="standard", sessions=1
        )
        self.github.edit_issue_labels(CHILD, add="ready-for-agent", remove=["human-review"])

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed", path="rerun.txt"))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_each_child_gets_its_own_count(self):
        self.new_two_child_run()
        findings = ScriptedResult("needs_correction", findings=("Handle empty carts.",), commit=True)
        child = [findings, ScriptedResult("committed")]

        set_child_session_limit(self.repository, standard=2)

        result = self.start(loop_core.STANDARD_RUN, *child * 2)

        self.assert_completed(result, sessions=4)
        self.assertEqual(self.session_children(), [CHILD] * 2 + [DEPENDENT] * 2)

    def test_reviewed_run_uses_the_reviewed_limit(self):
        findings = ScriptedResult("needs_correction", findings=("Handle empty carts.",))
        set_child_session_limit(self.repository, standard=1, reviewed=2)

        result = self.start(loop_core.REVIEWED_RUN, ScriptedResult("committed"), findings)

        self.assert_child_session_limit_stop(
            result, CHILD, role="issue correction", limit=2, mode="reviewed", sessions=2
        )

    def test_run_reviewed_by_the_setting_uses_the_reviewed_limit(self):
        set_review_by_default(self.repository)
        set_child_session_limit(self.repository, standard=1, reviewed=2)
        findings = ScriptedResult("needs_correction", findings=("Handle empty carts.",))
        self.sessions = ScriptedSessions(self.repository, ScriptedResult("committed"), findings)

        # No `--review`: the setting makes the run reviewed.
        result = start_run(self.repository, self.github, self.sessions, PARENT)

        self.assert_child_session_limit_stop(
            result, CHILD, role="issue correction", limit=2, mode="reviewed", sessions=2
        )

    def test_no_review_uses_the_standard_limit_despite_the_setting(self):
        set_review_by_default(self.repository)
        set_child_session_limit(self.repository, standard=1, reviewed=9)
        findings = ScriptedResult("needs_correction", findings=("Handle empty carts.",), commit=True)
        self.sessions = ScriptedSessions(self.repository, findings)

        result = start_run(self.repository, self.github, self.sessions, "--no-review", PARENT)

        self.assert_child_session_limit_stop(
            result, CHILD, role="issue correction", limit=1, mode="standard", sessions=1
        )

    def test_a_key_left_out_of_the_table_keeps_its_default(self):
        for mode in MODES:
            with self.subTest(mode.diagnostics_label):
                self.new_child_run()
                # Only the other mode's limit is configured, tighter than any worst case.
                set_child_session_limit(self.repository, **{"standard" if mode.review else "reviewed": 2})
                worst_case = worst_case_child_results(mode)

                result = self.start(mode, *worst_case)

                self.assert_implemented(result)
                self.assertEqual(len(self.sessions.requests), len(worst_case))

    def test_invalid_limit_is_rejected_before_any_session(self):
        for table, reason in (
            ('standard = "5"', "configuration.child_session_limit.standard must be an integer"),
            ("standard = 5.0", "configuration.child_session_limit.standard must be an integer"),
            ("reviewed = true", "configuration.child_session_limit.reviewed must be an integer"),
            ("standard = 0", "configuration.child_session_limit.standard must be at least 1"),
            ("reviewed = 1", "configuration.child_session_limit.reviewed must be at least 2"),
            ("review = 9", "configuration.child_session_limit has unknown keys: review"),
        ):
            with self.subTest(table):
                self.new_child_run()
                self.repository.edit_configuration(
                    "[verification]\n", f"[child_session_limit]\n{table}\n\n[verification]\n"
                )

                result = self.start(loop_core.STANDARD_RUN)

                self.assertEqual(result.exit_code, 1)
                self.assertIn(reason, result.stderr)
                self.assertEqual(self.sessions.requests, [])
                self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})

    def test_table_that_is_not_a_mapping_is_rejected(self):
        self.repository.edit_configuration(
            'trusted_authors = ["lmpaul"]\n', 'trusted_authors = ["lmpaul"]\nchild_session_limit = 5\n'
        )

        result = self.start(loop_core.STANDARD_RUN)

        self.assertEqual(result.exit_code, 1)
        self.assertIn("configuration.child_session_limit must be a mapping", result.stderr)
        self.assertEqual(self.sessions.requests, [])


class StopSummaryTests(VerificationTestCase):
    """A stopped run says why it stopped and what to do next, on GitHub and in the terminal."""

    def assert_stop_summary(self, result, *parts, outcome="stopped safely"):
        """Each part is in the parent's and the PR's terminal summary, and in the terminal."""
        published = [self.github.comments(PARENT)[-1]]
        if self.github.pull_request() is not None:
            published.append(self.github.pull_request().comments[-1])
        for summary in (*published, result.stderr):
            self.assertIn(f"- final outcome: {outcome}", summary)
            for part in parts:
                self.assertIn(part, summary)

    def test_child_not_ready_for_an_agent_is_named_with_its_label_and_next_action(self):
        self.github.add_issue(DEPENDENT, parent=PARENT, labels=("ready-for-human",))

        result = self.start(loop_core.REVIEWED_RUN, *passing_child_results(loop_core.REVIEWED_RUN))

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        self.assert_stop_summary(
            result,
            f"Stop reason: parent issue #{PARENT} has children remaining but none is runnable",
            f"Remaining children:\n- #{DEPENDENT}: labelled ready-for-human.",
            f"Next action:\n- #{DEPENDENT} is labelled ready-for-human: implement and close it, "
            f"or relabel it ready-for-agent, then run `macatia run --review {PARENT}` again.",
        )

    def test_child_waiting_on_an_unimplemented_blocker_names_the_blocker(self):
        self.new_run()
        self.github.add_issue(CHILD, parent=PARENT, labels=("needs-info",))
        self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[CHILD])

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_stop_summary(
            result,
            f"- #{CHILD}: labelled needs-info.\n- #{DEPENDENT}: waiting on #{CHILD}, which is not implemented.",
            f"- #{DEPENDENT} waits on #{CHILD}: implement or close it, then run `macatia run {PARENT}` again.",
        )

    def test_dependency_deadlock_names_the_blocked_children_and_their_blockers(self):
        self.new_run()
        self.github.add_issue(BLOCKER, parent=PARENT, blocked_by=[DEPENDENT])
        self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[BLOCKER])

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_stop_summary(
            result,
            "Stop reason: parent issue has a dependency deadlock",
            f"- #{BLOCKER}: part of a dependency deadlock, waiting on #{DEPENDENT}.",
            f"- #{DEPENDENT}: part of a dependency deadlock, waiting on #{BLOCKER}.",
            f"- #{BLOCKER} is part of a dependency deadlock with #{DEPENDENT}: remove a blocked-by "
            f"link from the cycle, relabel it ready-for-agent, then run `macatia run {PARENT}` again.",
        )

    def test_child_handed_to_human_review_links_its_handoff_comment(self):
        findings = ScriptedResult("needs_correction", findings=("Handle empty carts.",))
        for name, mode, results, reason in (
            ("blocked", loop_core.STANDARD_RUN, [ScriptedResult("blocked", "Needs a product decision.")],
             "implementation stopped with blocked: Needs a product decision."),
            ("failed", loop_core.STANDARD_RUN, [ScriptedResult("failed", "The toolchain crashed.")],
             "implementation stopped with failed: The toolchain crashed."),
            ("missing commit", loop_core.STANDARD_RUN, [findings, ScriptedResult("committed", commit=False)],
             "Issue correction did not create a new commit"),
            ("correction limit", loop_core.STANDARD_RUN, [findings, *[dataclasses.replace(findings, commit=True)] * 3],
             "correction limit of 3 exhausted before issue correction"),
        ):
            with self.subTest(name):
                self.new_child_run()

                result = self.start(mode, *results)

                handoff = self.github.latest_comment_url(CHILD)
                command = f"macatia run{' --review' if mode.review else ''} {PARENT}"
                self.assert_stop_summary(
                    result,
                    f"Stop reason: {reason}",
                    f"- #{CHILD}: handed to human review ({handoff}).",
                    f"- #{CHILD} was handed to human review: resolve what its handoff comment "
                    f"({handoff}) reports, then close it or relabel it ready-for-agent, "
                    f"and run `{command}` again.",
                )

    def test_exhausted_child_session_limit_says_the_next_run_gives_a_fresh_one(self):
        set_child_session_limit(self.repository, reviewed=2)

        result = self.start(
            loop_core.REVIEWED_RUN,
            ScriptedResult("committed"),
            ScriptedResult("needs_correction", findings=("Handle empty carts.",)),
        )

        self.assert_stop_summary(
            result,
            "- agent sessions: 2\n- infrastructure retries: 0\n",
            "Stop reason: child session limit exhausted before issue correction",
            f"- #{CHILD}: handed to human review ({self.github.latest_comment_url(CHILD)}).",
            f"Next action:\n- #{CHILD} was handed to human review because its child session "
            f"limit was exhausted: relabel it ready-for-agent, then run `macatia run --review "
            f"{PARENT}` again; each run gives a child a fresh child session limit.",
        )

    def test_exhausted_infrastructure_retries_give_their_reason_and_next_action(self):
        self.new_child_run(ci=("cancelled",))

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

        self.assert_stop_summary(
            result,
            "- agent sessions: 1\n- infrastructure retries: 3\n",
            f"Stop reason: infrastructure retries exhausted for canonical CI after 3 retries: "
            f"{CANONICAL_CHECK} concluded cancelled",
            f"Next action:\n- #{CHILD} was handed to human review because CI evidence could not "
            "be obtained within the infrastructure retries: once GitHub Actions is healthy, "
            f"relabel it ready-for-agent, then run `macatia run {PARENT}` again.",
        )

    def test_crash_names_the_exception_and_points_to_the_run_diagnostics(self):
        with self.assertRaises(ValueError):
            self.start(loop_core.STANDARD_RUN, ValueError("the adapter broke with token=hunter2"))

        directory = self.repository.diagnostics().relative_to(self.repository.path)
        summary = self.github.comments(PARENT)[-1]
        self.assertIn("- final outcome: crashed", summary)
        self.assertIn("Stop reason: ValueError: the adapter broke with token=[REDACTED]", summary)
        self.assertIn(f"Macatia crashed: inspect the run diagnostics in `{directory}`", summary)
        self.assertNotIn("hunter2", summary)

    def test_secrets_in_the_stop_reason_are_redacted(self):
        result = self.start(loop_core.STANDARD_RUN, MacatiaError("gh failed: token=hunter2 Bearer abc.def"))

        self.assert_stop_summary(
            result,
            "Stop reason: gh failed: token=[REDACTED] Bearer [REDACTED]",
            f"Next action:\n- Fix the stop reason, then run `macatia run {PARENT}` again.\n"
            f"- #{CHILD} is labelled agent-working: implement and close it, or relabel it "
            f"ready-for-agent, then run `macatia run {PARENT}` again.",
        )
        self.assertNotIn("hunter2", self.github.comments(PARENT)[-1])

    def test_rerun_command_keeps_the_configuration_path(self):
        (self.repository.path / "config").mkdir()
        git(self.repository.path, "mv", "macatia.toml", "config/macatia.toml")
        git(self.repository.path, "commit", "-m", "Move the Macatia configuration")
        self.sessions = ScriptedSessions(self.repository, ScriptedResult("blocked", "Stuck."))

        result = start_mode_run(
            loop_core.REVIEWED_RUN, self.repository, self.github, self.sessions,
            "--config", "config/macatia.toml", PARENT,
        )

        self.assert_stop_summary(
            result, f"run `macatia run --review --config config/macatia.toml {PARENT}` again."
        )

    def test_rerun_command_keeps_the_caffeinate_flag(self):
        self.sessions = ScriptedSessions(self.repository, ScriptedResult("blocked", "Stuck."))

        result = start_run(
            self.repository, self.github, self.sessions, PARENT, "--caffeinate", "--no-review"
        )

        self.assert_stop_summary(
            result, f"run `macatia run --no-review --caffeinate {PARENT}` again."
        )

    def test_rerun_command_keeps_the_no_caffeinate_flag(self):
        set_caffeinate_by_default(self.repository)
        self.sessions = ScriptedSessions(self.repository, ScriptedResult("blocked", "Stuck."))

        result = start_run(self.repository, self.github, self.sessions, "--no-caffeinate", PARENT)

        self.assert_stop_summary(result, f"run `macatia run --no-caffeinate {PARENT}` again.")

    def test_rerun_command_omits_the_sleep_lock_flags_when_the_setting_decided(self):
        set_caffeinate_by_default(self.repository)
        self.sessions = ScriptedSessions(self.repository, ScriptedResult("blocked", "Stuck."))

        result = start_run(self.repository, self.github, self.sessions, PARENT)

        self.assert_stop_summary(result, f"run `macatia run {PARENT}` again.")

    def test_completed_run_summary_has_no_stop_details(self):
        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

        self.assert_implemented(result)
        self.assertTrue(self.github.comments(PARENT)[-1].endswith("- final outcome: completed"))
        self.assertNotIn("Next action", result.stdout + result.stderr)


class AgentSessionTestCase(VerificationTestCase):
    """Macatia's real session handling, with scripted engine processes in place of the CLIs."""

    def start_engine(self, mode, *results, confirm=None):
        self.engine = ScriptedEngine(self.repository, *results)
        return start_mode_run(
            mode, self.repository, self.github, self.engine, PARENT, confirm=confirm,
        )

    def session_file(self, index, name):
        return (self.repository.session_directories()[index] / name).read_text()

    def assert_nothing_published(self):
        """Only the parent's terminal summary was written; the child is left agent working and the branch untouched."""
        self.assertEqual(self.github.labels(CHILD), {"agent-working"})
        self.assertEqual(self.github.reports(CHILD), [])
        (summary,) = self.github.comments(PARENT)
        self.assertIn("- final outcome: stopped safely", summary)
        self.assertIsNone(self.github.pull_request())
        with self.assertRaises(subprocess.CalledProcessError):
            self.repository.pushed_head()

    def assert_stopped_without_publishing(self, result, reason):
        self.assertEqual(result.exit_code, 1)
        self.assertIn(reason, result.stderr)
        self.assert_nothing_published()

    def assert_handed_to_human_review(self, result, reason, *handoff_parts):
        """The run stopped and handed the child to human review; returns the handoff comment."""
        self.assert_not_implemented(result, reason)
        self.assertEqual(self.github.labels(CHILD), {"human-review"})
        handoff = self.github.comments(CHILD)[-1]
        self.assertIn("This child remains open for human review.", handoff)
        self.assertIn(COMMITS_STAY, handoff)
        for part in handoff_parts:
            self.assertIn(part, handoff)
        return handoff


class OutcomeContractTests(AgentSessionTestCase):
    def test_results_breaking_the_outcome_contract_stop_without_publishing(self):
        completed_work = (
            "Updated the report fixture lookups after the internal ID change.",
            "Focused persistence test passed; acceptance gates completed.",
            "No push or GitHub state changes made.",
        )
        for name, run_options, results, reason in (
            (
                "needs_correction without findings",
                {},
                [EngineResult("needs_correction", commit=True)],
                "needs_correction requires at least one unresolved actionable finding",
            ),
            (
                "committed with remaining findings",
                {},
                [EngineResult("committed", findings=("Section freshness is missing.",))],
                "committed requires an empty findings array",
            ),
            (
                "completed work reported as findings",
                {"local_gate_results": ("fail",)},
                [EngineResult("committed"), EngineResult("committed", findings=completed_work)],
                "put completed work and successful checks in summary",
            ),
            (
                "a Claude Code result without findings",
                {"engines": {"implementation": "claude"}},
                [EngineResult(structured={"outcome": "committed", "summary": "Done"})],
                "agent structured output does not match the Macatia contract",
            ),
        ):
            with self.subTest(name):
                self.new_child_run(**run_options)

                result = self.start_engine(loop_core.STANDARD_RUN, *results)

                self.assert_stopped_without_publishing(result, reason)
                self.assertEqual(len(self.engine.calls), len(results))

    def test_no_changes_is_published_after_a_clean_independent_review(self):
        result = self.start_engine(
            loop_core.REVIEWED_RUN, EngineResult("no_changes"), EngineResult("no_changes")
        )

        evidence = self.assert_implemented(result)
        self.assertEqual(len(self.engine.calls), 2)
        self.assertEqual(self.repository.head(), self.repository.base_sha)
        self.assertEqual(self.repository.pushed_head(), self.repository.base_sha)
        self.assertIn(f"`{self.repository.base_sha}`", evidence)

    def test_failed_session_with_a_commit_is_a_terminal_stop(self):
        result = self.start_engine(loop_core.STANDARD_RUN, EngineResult("failed", "The build tool crashed.", commit=True))

        self.assert_handed_to_human_review(
            result,
            "implementation stopped with failed: The build tool crashed.",
            "implementation reported failed: The build tool crashed.",
        )
        self.assertEqual(len(self.engine.calls), 1)
        self.assertIsNone(self.github.pull_request())

    def test_claimed_commit_without_a_git_commit_hands_the_child_to_human_review(self):
        result = self.start_engine(loop_core.STANDARD_RUN, EngineResult("committed", commit=False))

        self.assert_handed_to_human_review(
            result,
            "implementation claimed committed without creating a Git commit",
            "Macatia stopped because implementation claimed committed without creating a Git commit.",
        )
        self.assertEqual(len(self.engine.calls), 1)
        self.assertIsNone(self.github.pull_request())
        with self.assertRaises(subprocess.CalledProcessError):
            self.repository.pushed_head()

    def test_dirty_agent_output_cannot_be_published_and_keeps_its_evidence(self):
        result = self.start_engine(loop_core.STANDARD_RUN, EngineResult("committed", dirty=True))

        self.assert_handed_to_human_review(result, "worktree is dirty after implementation")
        self.assertIsNone(self.github.pull_request())
        self.assertTrue((self.repository.path / "uncommitted-agent-output.txt").is_file())
        (record,) = self.repository.session_records()
        self.assertEqual(record["structured_result"]["outcome"], "committed")
        self.assertIn(
            "Rejected: worktree is dirty after implementation", self.session_file(0, "validation.txt")
        )


class InvalidOutputTests(AgentSessionTestCase):
    def test_invalid_structured_output_stops_without_publishing_and_is_retained(self):
        for name, engine, session, reason in (
            ("Codex without a result", "codex", EngineResult(writes_result=False),
             "agent did not return valid structured output"),
            ("Claude Code printing invalid JSON", "claude", EngineResult(stdout="Not JSON output"),
             "Claude Code did not return valid JSON output"),
        ):
            with self.subTest(name):
                self.new_child_run(engines={"implementation": engine})

                result = self.start_engine(loop_core.REVIEWED_RUN, session)

                self.assert_stopped_without_publishing(result, reason)
                self.assertEqual(self.engine.engines(), [engine])
                (record,) = self.repository.session_records()
                self.assertEqual((record["engine"], record["returncode"]), (engine, 0))
                self.assertIsNone(record["structured_result"])
                self.assertIn(f"Rejected: {reason}", self.session_file(0, "validation.txt"))
                if session.stdout is not None:
                    self.assertIn(session.stdout, self.session_file(0, "stdout.txt"))


class NonZeroExitTests(AgentSessionTestCase):
    def test_nonzero_exit_with_a_valid_committed_result_is_still_verified(self):
        result = self.start_engine(loop_core.STANDARD_RUN, EngineResult("committed", returncode=1))

        self.assert_implemented(result)
        (record,) = self.repository.session_records()
        self.assertEqual((record["returncode"], record["structured_result"]["outcome"]), (1, "committed"))
        self.assertIn(
            "Accepted a valid committed result despite the non-zero Codex exit.",
            self.session_file(0, "validation.txt"),
        )
        self.assertEqual(self.github.checked_heads, [self.repository.head()])

    def test_nonzero_exit_without_a_committed_result_cannot_change_issue_state(self):
        for session in (
            EngineResult("blocked", "Needs a decision."),
            EngineResult("needs_correction", findings=("Complete the journey.",), commit=True),
        ):
            with self.subTest(session.outcome):
                self.new_child_run()

                result = self.start_engine(loop_core.STANDARD_RUN, dataclasses.replace(session, returncode=1))

                self.assert_stopped_without_publishing(
                    result, "implementation exited non-zero without a committed outcome"
                )
                self.assertEqual(len(self.engine.calls), 1)

    def test_claude_code_failure_without_a_result_reports_its_exit_code_and_stderr(self):
        self.new_child_run(engines={"implementation": "claude"})

        result = self.start_engine(
            loop_core.REVIEWED_RUN,
            EngineResult(
                returncode=1,
                commit=False,
                writes_result=False,
                stdout="",
                stderr="Error: --json-schema is not a valid JSON Schema\n",
            ),
        )

        self.assert_stopped_without_publishing(
            result, "Claude Code exited with code 1: Error: --json-schema is not a valid JSON Schema"
        )
        self.assertEqual(self.engine.engines(), ["claude"])


class HumanReviewEvidenceTests(AgentSessionTestCase):
    def test_blocked_sessions_leave_precise_human_review_evidence(self):
        decision = "Needs a product decision on retention."
        for role, run_options, results in (
            ("implementation", {}, [EngineResult("blocked", decision)]),
            ("CI correction", {"ci": ("failure",)}, [EngineResult("committed"), EngineResult("blocked", decision)]),
        ):
            with self.subTest(role):
                self.new_child_run(**run_options)

                result = self.start_engine(loop_core.STANDARD_RUN, *results)

                self.assert_handed_to_human_review(
                    result,
                    f"{role} stopped with blocked: {decision}",
                    f"Macatia stopped because {role} reported blocked: {decision}",
                )
                self.assertEqual(len(self.engine.calls), len(results))

    def test_findings_are_preserved_in_full_and_redacted_for_correction_and_handoff(self):
        details = "Section details. " * 100
        finding = f"First requirement.\n{details}\nLast requirement. token=fixture-secret"

        result = self.start_engine(
            loop_core.STANDARD_RUN,
            EngineResult("needs_correction", findings=(finding,), commit=True),
            EngineResult("no_changes"),
        )

        handoff = self.assert_handed_to_human_review(result, "Issue correction did not create a new commit")
        correction_prompt = self.engine.calls[1].prompt
        self.assertIn("Complete the remaining requirements", correction_prompt)
        for diagnostic in (correction_prompt, handoff):
            self.assertIn("First requirement.", diagnostic)
            self.assertIn(details, diagnostic)
            self.assertIn("Last requirement. token=[REDACTED]", diagnostic)
            self.assertNotIn("fixture-secret", diagnostic)


class SessionSummaryTests(AgentSessionTestCase):
    def test_long_and_multi_sentence_summaries_do_not_stop_a_verified_run(self):
        multi_sentence = "One sentence. Two sentences. Three sentences."
        for name, summary, recorded in (
            ("multi-sentence", multi_sentence, multi_sentence),
            ("long", "x" * 501, loop_core.LONG_AGENT_SUMMARY),
        ):
            with self.subTest(name):
                self.new_child_run()

                result = self.start_engine(
                    loop_core.REVIEWED_RUN, EngineResult("committed", summary), EngineResult("no_changes")
                )

                self.assert_implemented(result)
                implementation, _review = self.repository.session_records()
                self.assertEqual(implementation["structured_result"]["summary"], recorded)
                self.assertIn(f"Previous session summary: {recorded}\n", self.engine.calls[1].prompt)

    def test_each_session_receives_the_previous_session_summary(self):
        self.new_child_run(local_gate_results=("fail", "pass"), ci=("failure", "success"))

        result = self.start_engine(
            loop_core.STANDARD_RUN,
            EngineResult("committed", "Implemented the journey."),
            EngineResult("committed", "Repaired the failing local gate."),
            EngineResult("committed", "Repaired the failing CI check."),
        )

        self.assert_implemented(result)
        prompts = [call.prompt for call in self.engine.calls]
        self.assertNotIn("Previous session summary:", prompts[0])
        self.assertIn("Previous session summary: Implemented the journey.\n", prompts[1])
        self.assertIn("Previous session summary: Repaired the failing local gate.\n", prompts[2])


class EngineSelectionTests(AgentSessionTestCase):
    def test_each_role_can_use_either_engine(self):
        for name, engines, expected in (
            ("Codex for every role", {}, ["codex"] * 3),
            ("Claude Code implementation with Codex review", {"implementation": "claude"},
             ["claude", "codex", "codex"]),
            ("Claude Code for every role", {"implementation": "claude", "review": "claude", "correction": "claude"},
             ["claude"] * 3),
        ):
            with self.subTest(name):
                self.new_child_run(local_gate_results=("fail", "pass"), engines=engines)

                result = self.start_engine(
                    loop_core.REVIEWED_RUN,
                    EngineResult("committed"),
                    EngineResult("committed"),
                    EngineResult("no_changes"),
                )

                self.assert_implemented(result)
                self.assertEqual(self.engine.engines(), expected)
                self.assertEqual(
                    [record["engine"] for record in self.repository.session_records()], expected
                )
                implementation, gate_correction, review = (call.prompt for call in self.engine.calls)
                self.assertIn(f"implement issue #{CHILD}\n", implementation)
                self.assertIn("Repair the failing local gate", gate_correction)
                self.assertIn("Review the exact", review)

    def test_sessions_are_announced_with_their_engine_and_profile(self):
        self.new_child_run(local_gate_results=("fail", "pass"), engines={"implementation": "claude"})

        result = self.start_engine(loop_core.STANDARD_RUN, EngineResult("committed"), EngineResult("committed"))

        self.assert_implemented(result)
        self.assertIn(
            f"🤖 Starting Claude Code implementation session (Terra - Medium) for child #{CHILD}.",
            result.stdout,
        )
        self.assertIn(
            f"🤖 Starting Codex local gate correction session (Terra - Medium) for child #{CHILD}.",
            result.stdout,
        )
        self.assertRegex(
            result.stdout,
            rf"🤖 Codex local gate correction session \(Terra - Medium\) finished for child #{CHILD} "
            r"in \d{2}:\d{2}:\d{2} \(outcome: committed; commits: 1; files changed: 1; \+1/-0\)\.",
        )
        self.assertIn(
            f"✅ Completed child #{CHILD}\n🔄 Selecting the next runnable child.\n", result.stdout
        )
        correction = self.engine.calls[1]
        self.assertEqual(correction.option("--model"), "gpt-5.6-terra")
        self.assertIn('model_reasoning_effort="medium"', correction.arguments)
        self.assertIn("Use an empty findings array for committed or no_changes.", correction.prompt)
        self.assertNotIn("$implement", correction.prompt)

    def test_configuration_without_an_engine_still_uses_codex(self):
        self.new_child_run(engines=dict.fromkeys(("implementation", "review", "correction")))

        result = self.start_engine(loop_core.REVIEWED_RUN, EngineResult("committed"), EngineResult("no_changes"))

        self.assert_implemented(result)
        self.assertEqual(self.engine.engines(), ["codex", "codex"])


BACKUP_PROFILE = 'engine = "claude"\nname = "opus"\ndisplay_name = "Opus"\nreasoning_effort = "high"'
FAILED_ENGINE_STDERR = "Error: You have hit your usage limit.\n"
# One session of each engine failure kind, with the engines of the regular profiles it runs on.
ENGINE_FAILURES = (
    ("Claude Code error envelope", {"implementation": "claude"},
     EngineResult(is_error=True, commit=False)),
    ("Claude Code invalid JSON", {"implementation": "claude"},
     EngineResult(stdout="Not JSON output", commit=False)),
    ("Claude Code without a structured result", {"implementation": "claude"},
     EngineResult(writes_result=False, commit=False)),
    ("Codex without a result file", {}, EngineResult(writes_result=False, commit=False)),
    ("result breaking the Macatia contract", {},
     EngineResult(structured={"outcome": "committed", "summary": "Done"})),
    ("non-zero exit without a committed outcome", {},
     EngineResult("blocked", "Usage limit.", returncode=1, stderr=FAILED_ENGINE_STDERR)),
)


class BackupTakeoverTests(AgentSessionTestCase):
    """A configured backup replays a session that ended in an engine failure and runs the rest of the run."""

    def new_child_run(self, *, backup=True, **options):
        super().new_child_run(**options)
        if backup:
            self.repository.edit_configuration(
                "\n[verification]", f"\n[models.backup]\n{BACKUP_PROFILE}\n\n[verification]"
            )
        self.start_head = self.repository.head()

    def assert_backup_call(self, call):
        self.assertEqual(call.engine, "claude")
        self.assertEqual((call.option("--model"), call.option("--effort")), ("opus", "high"))

    def published_summaries(self, result, stream="stdout"):
        """The run summary on the parent, on the cumulative draft PR when there is one, and in the terminal."""
        marker = "🏁 Macatia summary"
        summaries = [
            next(comment for comment in reversed(self.github.comments(PARENT)) if marker in comment),
            getattr(result, stream),
        ]
        pull = self.github.pull_request()
        if pull is not None:
            summaries.append(pull.comments[-1])
        return summaries

    def assert_backup_line(self, result, line, stream="stdout"):
        for summary in self.published_summaries(result, stream):
            self.assertIn(f"\n{line}\n", summary)

    def test_each_engine_failure_kind_is_replayed_by_the_backup_and_the_run_continues(self):
        for name, engines, failure in ENGINE_FAILURES:
            with self.subTest(name):
                self.new_child_run(engines=engines)

                result = self.start_engine(loop_core.STANDARD_RUN, failure, EngineResult("committed"))

                self.assert_implemented(result)
                failed, replay = self.engine.calls
                self.assertEqual(failed.engine, engines.get("implementation", "codex"))
                self.assert_backup_call(replay)
                # The same request: Claude Code only spells the implement skill differently.
                self.assertEqual(replay.prompt, failed.prompt.replace("$implement ", "/implement ", 1))
                self.assertIn("- agent sessions: 2\n", result.stdout)

    def test_replay_starts_from_the_head_the_failed_session_started_from(self):
        result = self.start_engine(
            loop_core.STANDARD_RUN,
            EngineResult("blocked", "Usage limit.", returncode=1, commit=True, dirty=True),
            EngineResult("committed"),
        )

        self.assert_implemented(result)
        (commit,) = self.repository.commits_since(self.start_head)
        self.assertEqual(git(self.repository.path, "log", "-1", "--format=%s", commit), "claude session 2")
        self.assertFalse((self.repository.path / "codex-session-1.txt").exists())
        self.assertFalse((self.repository.path / "uncommitted-agent-output.txt").exists())
        self.assertTrue(self.repository.is_clean())
        self.assertEqual(self.repository.pushed_head(), self.repository.head())
        self.assertNotIn(
            "codex session 1", git(self.repository.origin, "log", "--all", "--format=%s")
        )

    def test_backup_runs_every_later_session_of_the_run(self):
        self.new_child_run(local_gate_results=("fail", "pass"), ci=("failure", "success"))
        self.github.add_issue(DEPENDENT, parent=PARENT)
        finding = ("Handle empty carts.",)

        result = self.start_engine(
            loop_core.REVIEWED_RUN,
            EngineResult(writes_result=False, commit=False),
            EngineResult("committed"),
            EngineResult("committed"),
            EngineResult("needs_correction", findings=finding),
            EngineResult("committed"),
            EngineResult("no_changes"),
            EngineResult("committed"),
            EngineResult("no_changes"),
            EngineResult("committed"),
            EngineResult("no_changes"),
        )

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        self.assertEqual(self.github.labels(DEPENDENT), {"implemented"})
        self.assertEqual(self.engine.calls[0].engine, "codex")
        for call in self.engine.calls[1:]:
            self.assert_backup_call(call)
        roles = [record["role"] for record in self.repository.session_records()]
        self.assertEqual(roles, [
            "implementation", "implementation", "local gate correction", "review",
            "issue correction", "review", "CI correction", "review", "implementation", "review",
        ])
        for kind in ("implementation", "local gate correction", "review", "issue correction", "CI correction"):
            self.assertIn(
                f"🤖 Starting Claude Code {kind} session (Opus - High) for child #{CHILD}.",
                result.stdout,
            )
        self.assertIn(
            f"🤖 Starting Claude Code implementation session (Opus - High) for child #{DEPENDENT}.",
            result.stdout,
        )

    def test_failed_review_session_is_replayed_by_the_backup(self):
        result = self.start_engine(
            loop_core.REVIEWED_RUN,
            EngineResult("committed"),
            EngineResult(writes_result=False, commit=False),
            EngineResult("no_changes"),
        )

        self.assert_implemented(result)
        self.assertEqual(self.engine.engines(), ["codex", "codex", "claude"])
        self.assert_backup_call(self.engine.calls[2])
        self.assertEqual(self.engine.calls[2].prompt, self.engine.calls[1].prompt)
        self.assertIn(
            f"⚠️ Codex engine failure in the review session for child #{CHILD}", result.stdout
        )

    def test_run_summary_reports_the_backup_takeover(self):
        result = self.start_engine(
            loop_core.REVIEWED_RUN,
            EngineResult("committed"),
            EngineResult(writes_result=False, commit=False),
            EngineResult("no_changes"),
        )

        self.assert_implemented(result)
        self.assertIn("🏁 Macatia summary", self.github.pull_request().comments[-1])
        self.assert_backup_line(
            result,
            f"- backup: Claude Code Opus - High took over after a Codex engine failure "
            f"(review session, child #{CHILD})",
        )

    def test_run_summary_without_a_switch_has_no_backup_line(self):
        for backup in (True, False):
            with self.subTest(backup=backup):
                self.new_child_run(backup=backup)

                result = self.start_engine(loop_core.STANDARD_RUN, EngineResult("committed"))

                self.assert_implemented(result)
                for summary in self.published_summaries(result):
                    self.assertIn("- infrastructure retries: 0\n- total duration: ", summary)
                    self.assertNotIn("- backup:", summary)

    def test_switch_is_announced_and_recorded_in_the_run_diagnostics(self):
        result = self.start_engine(
            loop_core.STANDARD_RUN,
            EngineResult("blocked", "Usage limit.", returncode=1, stderr=FAILED_ENGINE_STDERR),
            EngineResult("committed"),
        )

        self.assert_implemented(result)
        self.assertIn(
            f"⚠️ Codex engine failure in the implementation session for child #{CHILD}: "
            "implementation exited non-zero without a committed outcome "
            "(Codex exited with code 1: Error: You have hit your usage limit.). "
            "Switching to the backup, Claude Code (Opus - High).\n",
            result.stdout,
        )
        self.assertIn(
            "✅ The backup took over and runs every session for the rest of the run.\n", result.stdout
        )
        failed, replay = self.repository.session_records()
        self.assertEqual((failed["engine"], failed["returncode"]), ("codex", 1))
        self.assertIn("Rejected: implementation exited non-zero", self.session_file(0, "validation.txt"))
        self.assertEqual(replay["engine"], "claude")
        run_record = json.loads((self.repository.diagnostics() / "run.json").read_text())
        self.assertEqual(run_record["backup_switch"], {
            "failed_engine": "codex",
            "role": "implementation",
            "child": CHILD,
            "failure": "implementation exited non-zero without a committed outcome "
                       "(Codex exited with code 1: Error: You have hit your usage limit.)",
            "backup": {
                "engine": "claude", "model": "opus", "display_name": "Opus", "reasoning_effort": "high",
            },
        })

    def test_engine_failure_of_the_backup_stops_the_run_naming_both_failures(self):
        result = self.start_engine(
            loop_core.STANDARD_RUN,
            EngineResult(writes_result=False, commit=False),
            EngineResult(is_error=True, commit=False),
        )

        self.assert_stopped_without_publishing(
            result,
            "engine failure of the backup (Claude Code): Claude Code reported an error; inspect "
            "retained session output; after the Codex engine failure: agent did not return valid "
            "structured output",
        )
        self.assertEqual(self.engine.engines(), ["codex", "claude"])
        self.assertNotIn("✅ The backup took over", result.stdout)
        self.assert_backup_line(
            result,
            f"- backup: Claude Code Opus - High took over after a Codex engine failure "
            f"(implementation session, child #{CHILD})",
            stream="stderr",
        )

    def test_new_run_resuming_an_agent_working_child_starts_on_the_regular_profiles(self):
        self.start_engine(
            loop_core.STANDARD_RUN,
            EngineResult(writes_result=False, commit=False),
            EngineResult(writes_result=False, commit=False),
        )
        self.assertEqual(self.github.labels(CHILD), {"agent-working"})

        result = self.start_engine(
            loop_core.STANDARD_RUN, EngineResult("committed"), confirm=ScriptedConfirm(RESUME)
        )

        self.assert_implemented(result)
        self.assertEqual(self.engine.engines(), ["codex"])
        self.assertEqual(self.engine.calls[0].option("--model"), "gpt-5.6-terra")

    def test_agent_outcomes_git_effect_rejections_and_interruptions_never_start_the_backup(self):
        for name, results, expected_exit in (
            ("failed", [EngineResult("failed", "The build tool crashed.")], 1),
            ("blocked", [EngineResult("blocked", "Needs a decision.")], 1),
            ("no_changes", [EngineResult("no_changes")], 0),
            ("needs_correction", [
                EngineResult("needs_correction", findings=("Handle empty carts.",)),
                EngineResult("committed"),
            ], 0),
            ("Git effect rejection", [EngineResult("committed", commit=False)], 1),
            ("Ctrl-C", [KeyboardInterrupt()], 130),
            ("SIGTERM", [loop_core.Interrupted(signal.SIGTERM)], 143),
        ):
            with self.subTest(name):
                self.new_child_run()

                result = self.start_engine(loop_core.STANDARD_RUN, *results)

                self.assertEqual(result.exit_code, expected_exit, result.stderr)
                self.assertEqual(self.engine.engines(), ["codex"] * len(results))
                self.assertNotIn("backup", result.stdout)

    def test_nonzero_exit_with_a_valid_committed_result_is_accepted_without_switching(self):
        result = self.start_engine(loop_core.STANDARD_RUN, EngineResult("committed", returncode=1))

        self.assert_implemented(result)
        self.assertEqual(self.engine.engines(), ["codex"])
        self.assertNotIn("backup", result.stdout)

    def test_failed_session_and_its_replay_count_toward_the_child_session_limit(self):
        self.new_child_run()

        set_child_session_limit(self.repository, reviewed=2)

        result = self.start_engine(
            loop_core.REVIEWED_RUN,
            EngineResult(writes_result=False, commit=False),
            EngineResult("committed"),
        )

        self.assert_handed_to_human_review(
            result,
            "child session limit exhausted before review",
            "child session limit of 2 (reviewed run) was exhausted",
        )
        self.assertEqual(self.engine.engines(), ["codex", "claude"])

    def test_engine_failure_on_the_last_allowed_session_exhausts_the_limit_instead_of_replaying(self):
        set_child_session_limit(self.repository, standard=1)
        self.start_head = self.repository.head()

        result = self.start_engine(
            loop_core.STANDARD_RUN,
            EngineResult("blocked", "Usage limit.", returncode=1, commit=True, dirty=True),
        )

        self.assert_handed_to_human_review(
            result,
            "child session limit exhausted before implementation",
            "Macatia stopped before implementation because the child session limit of 1 "
            "(standard run) was exhausted.",
            "implementation exited non-zero without a committed outcome",
        )
        self.assertEqual(self.engine.engines(), ["codex"])
        self.assertNotIn("backup", result.stdout)
        self.assertEqual(self.repository.head(), self.start_head)
        self.assertTrue(self.repository.is_clean())
        self.assertIn(
            f"⚠️ Codex engine failure in the implementation session for child #{CHILD}: "
            "implementation exited non-zero without a committed outcome",
            result.stdout,
        )
        self.assert_backup_line(
            result,
            f"- engine failure: Codex in the implementation session for child #{CHILD}",
            stream="stderr",
        )
        for summary in self.published_summaries(result, stream="stderr"):
            self.assertNotIn("backup", summary)
        run_record = json.loads((self.repository.diagnostics() / "run.json").read_text())
        self.assertNotIn("backup_switch", run_record)

    def test_failed_correction_and_its_replay_count_as_one_correction(self):
        self.new_child_run(local_gate_results=("fail", "fail", "fail", "pass"))

        result = self.start_engine(
            loop_core.STANDARD_RUN,
            EngineResult("committed"),
            EngineResult(writes_result=False, commit=False),
            EngineResult("committed"),
            EngineResult("committed"),
            EngineResult("committed"),
        )

        self.assert_implemented(result)
        self.assertEqual(self.engine.engines(), ["codex", "codex", "claude", "claude", "claude"])
        self.assertIn("Repair the failing local gate", self.engine.calls[2].prompt)


# A run target without sub-issues: every run test starts its run on PARENT.
STANDALONE = PARENT


class StandaloneRunTestCase(VerificationTestCase):
    """A run on a standalone issue: the issue itself is the run's only child."""

    def new_child_run(self, *, issue_labels=("ready-for-agent",), backup=False, **options):
        super().new_child_run(**options)
        self.github = FakeGitHub()
        self.github.add_issue(STANDALONE, labels=issue_labels)
        if backup:
            self.repository.edit_configuration(
                "\n[verification]", f"\n[models.backup]\n{BACKUP_PROFILE}\n\n[verification]"
            )

    def start_observing_labels(self, mode, *results):
        """Start a run that records the issue's labels as each agent session starts."""
        observed = []
        self.before_session = lambda request: observed.append(
            (request.role, request.child, self.github.labels(STANDALONE))
        )
        return self.start(mode, *results), observed

    def start_engine(self, mode, *results):
        self.engine = ScriptedEngine(self.repository, *results)
        return start_mode_run(mode, self.repository, self.github, self.engine, STANDALONE)

    def assert_standalone_implemented(self, result, *, sessions):
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertIn(
            f"🎉 Standalone issue #{STANDALONE} is implemented; the pull request is ready for review.",
            result.stdout,
        )
        self.assertNotIn("children of", result.stdout)
        self.assertIn(f"🏁 Macatia summary for standalone issue #{STANDALONE}:\n", result.stdout)
        self.assertIn(f"- agent sessions: {sessions}\n", result.stdout)
        self.assertEqual(self.github.labels(STANDALONE), {"implemented", "standalone"})
        comments = self.github.comments(STANDALONE)
        self.assertTrue(any("Macatia verified implementation commits" in body for body in comments))
        self.assertFalse(any("parent-completion" in body for body in comments))
        self.assertIn("- final outcome: completed", comments[-1])
        pull = self.github.pull_request()
        self.assertEqual(pull.title, f"Implement Macatia issue #{STANDALONE}")
        self.assertIn("Human merge required", pull.body)
        self.assertNotIn("Closes", pull.body)
        self.assertFalse(pull.is_draft)

    def assert_refused(self, result, reason, labels):
        """The run stopped at startup: nothing commented, labelled, pushed or run."""
        self.assertEqual(result.exit_code, 1)
        self.assertIn(reason, result.stderr)
        self.assertEqual(self.sessions.requests, [])
        self.assertEqual(self.github.comments(STANDALONE), [])
        self.assertEqual(self.github.labels(STANDALONE), set(labels))
        self.assertIsNone(self.github.pull_request())
        with self.assertRaises(subprocess.CalledProcessError):
            self.repository.pushed_head()


class StandaloneRunTests(StandaloneRunTestCase):
    def test_standard_run_implements_the_issue_and_marks_the_pull_request_ready(self):
        result, observed = self.start_observing_labels(
            loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN)
        )

        self.assert_standalone_implemented(result, sessions=1)
        self.assertEqual(observed, [("implementation", STANDALONE, {"agent-working", "standalone"})])

    def test_reviewed_run_includes_the_review_session(self):
        result, observed = self.start_observing_labels(
            loop_core.REVIEWED_RUN, *passing_child_results(loop_core.REVIEWED_RUN)
        )

        self.assert_standalone_implemented(result, sessions=2)
        self.assertEqual(
            observed,
            [
                ("implementation", STANDALONE, {"agent-working", "standalone"}),
                ("review", STANDALONE, {"agent-working", "standalone"}),
            ],
        )

    def test_issue_never_gets_work_started_or_a_parent_completion_comment(self):
        self.new_child_run(issue_labels=("ready-for-agent", "documentation"))

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(self.github.labels(STANDALONE), {"implemented", "standalone", "documentation"})
        self.assertFalse(
            any("Macatia observed all" in body for body in self.github.comments(STANDALONE))
        )


class StandaloneFinalCICheckTests(StandaloneRunTestCase):
    def test_red_or_unavailable_final_ci_hands_the_standalone_issue_to_human_review(self):
        for ci, shown in (
            ("failure", f"{CANONICAL_CHECK} concluded failure"),
            ("cancelled", "Last infrastructure diagnostic"),
        ):
            with self.subTest(ci):
                self.repository = RunRepository(self, ci_checks=False, final_ci_check=True)
                self.github = FakeGitHub(ci=(ci,))
                self.github.add_issue(STANDALONE, labels=("ready-for-agent",))

                result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

                self.assertEqual(result.exit_code, 1)
                self.assertEqual(
                    self.github.labels(STANDALONE), {"implemented", "standalone", "human-review"}
                )
                self.assertTrue(self.github.pull_request().is_draft)
                comments = self.github.comments(STANDALONE)
                self.assertTrue(comments[-2].startswith(f"{HANDOFF_MARKER}\nMacatia stopped"))
                self.assertIn(shown, comments[-2])
                self.assertIn("- final outcome: stopped safely", comments[-1])
                self.assertIn("then mark the pull request ready for review", comments[-1])

    def test_green_final_ci_marks_the_pull_request_ready(self):
        self.repository = RunRepository(self, ci_checks=False, final_ci_check=True)

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"))

        self.assert_standalone_implemented(result, sessions=1)
        head = self.repository.head()
        self.assertEqual(self.github.checked_heads, [head])
        self.assertIn(FINAL_CI_GREEN.format(head=head), self.github.comments(STANDALONE)[-1])


class StandaloneStartTests(StandaloneRunTestCase):
    def test_issue_that_is_not_ready_for_an_agent_is_refused_with_no_write(self):
        for issue_labels, reason in (
            (("needs-info",), "labelled needs-info"),
            (("ready-for-agent", "needs-info"), "labelled needs-info"),
            (("implemented",), "labelled implemented"),
            (("human-review",), "labelled human-review"),
            (("ready-for-agent", "human-review"), "labelled human-review"),
            ((), "not labelled ready-for-agent"),
            (("documentation",), "not labelled ready-for-agent"),
        ):
            with self.subTest(issue_labels=issue_labels):
                self.new_child_run(issue_labels=issue_labels)

                result = self.start(loop_core.STANDARD_RUN)

                self.assert_refused(
                    result,
                    f"standalone issue #{STANDALONE} is not ready for an agent: {reason}",
                    issue_labels,
                )

    def test_closed_issue_is_refused_with_no_write(self):
        self.github.issues[STANDALONE]["state"] = "CLOSED"

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_refused(
            result, f"standalone issue #{STANDALONE} is closed", ("ready-for-agent",)
        )

    def test_untrusted_issue_is_refused_with_no_write(self):
        self.github.issues[STANDALONE]["author"] = {"login": "outside-contributor"}

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_refused(
            result, f"issue #{STANDALONE} must be authored by one of: @{TRUSTED_LOGIN}",
            ("ready-for-agent",),
        )

    def test_unsatisfied_blocker_is_named_and_the_issue_refused_with_no_write(self):
        self.github.add_issue(BLOCKER)
        self.github.add_issue(DEPENDENT, labels=("implemented",))
        self.github.blocked_by[STANDALONE] = [BLOCKER, DEPENDENT]

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_refused(
            result,
            f"standalone issue #{STANDALONE} waits on #{BLOCKER}, #{DEPENDENT}, neither "
            "closed nor implemented on this branch",
            ("ready-for-agent",),
        )
        self.assertEqual(self.github.comments(BLOCKER), [])

    def test_failed_blocker_lookup_refuses_with_its_own_reason(self):
        verified_head = self.commit_off_the_branch()
        self.github.add_issue(
            BLOCKER, labels=("implemented",),
            comments=[implementation_evidence(loop_core.STANDARD_RUN, verified_head)],
        )
        self.github.blocked_by[STANDALONE] = [BLOCKER]
        self.github.commit_pulls[verified_head] = MacatiaError("gh: Server Error (HTTP 502)")

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_refused(result, "gh: Server Error (HTTP 502)", ("ready-for-agent",))
        self.assertNotIn("neither closed nor implemented", result.stderr)

    def test_satisfied_blockers_let_it_start(self):
        verified_head = self.commit_prior_child()
        self.github.add_issue(CLOSED_BLOCKER, state="closed")
        self.github.add_issue(
            BLOCKER, labels=("implemented",),
            comments=[implementation_evidence(loop_core.STANDARD_RUN, verified_head)],
        )
        self.github.blocked_by[STANDALONE] = [CLOSED_BLOCKER, BLOCKER]

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_standalone_implemented(result, sessions=1)
        self.assertEqual(self.session_children(), [STANDALONE])


class StandaloneStopTests(StandaloneRunTestCase):
    def test_correction_limit_hands_it_to_human_review_and_a_requeued_issue_resumes(self):
        findings = ScriptedResult("needs_correction", findings=("Handle empty carts.",))

        result = self.start(
            loop_core.STANDARD_RUN, findings, *[dataclasses.replace(findings, commit=True)] * 3
        )

        self.assertEqual(result.exit_code, 1)
        self.assertIn("correction limit of 3 exhausted before issue correction", result.stderr)
        self.assertEqual(self.github.labels(STANDALONE), {"human-review", "standalone"})
        self.assertIn(COMMITS_STAY, self.github.reports(STANDALONE)[-2])
        self.assertIn("- final outcome: stopped safely", self.github.comments(STANDALONE)[-1])

        self.github.edit_issue_labels(STANDALONE, add="ready-for-agent", remove=["human-review"])
        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_standalone_implemented(result, sessions=1)

    def test_left_agent_working_gets_the_resume_or_stop_choice(self):
        for answer, exit_code, final_labels in (
            (STOP, 1, {"agent-working"}),
            (RESUME, 0, {"implemented", "standalone"}),
        ):
            with self.subTest(resume=answer):
                self.new_child_run(issue_labels=("agent-working",))
                confirm = ScriptedConfirm(answer)

                result = self.start(
                    loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN),
                    confirm=confirm,
                )

                self.assertEqual(result.exit_code, exit_code, result.stderr)
                self.assertEqual(confirm.questions, [f"Resume #{STANDALONE}?"])
                self.assertEqual(self.github.labels(STANDALONE), final_labels)

    def test_published_but_unlabelled_issue_is_recovered_without_another_session(self):
        for name, stop in (
            ("process death", lambda: setattr(
                self.github, "interrupt_after_comment_on", STANDALONE
            )),
            ("crash", lambda: setattr(self.github, "after_comment", self.crash_after_evidence)),
        ):
            with self.subTest(name):
                self.new_child_run()
                self.github.interrupt_after_comment_containing = (
                    loop_core.STANDARD_RUN.implementation_evidence_prefix
                )
                stop()
                with self.assertRaises((RunInterrupted, ValueError)):
                    self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))
                self.assertEqual(self.github.labels(STANDALONE), {"agent-working", "standalone"})
                self.github.after_comment = None
                confirm = ScriptedConfirm(STOP)

                result = self.start(loop_core.STANDARD_RUN, confirm=confirm)

                self.assert_standalone_implemented(result, sessions=0)
                self.assertEqual(confirm.questions, [])

    def crash_after_evidence(self, number, body):
        if loop_core.STANDARD_RUN.implementation_evidence_prefix in body:
            raise ValueError("the label write never happened")


class StandaloneMarkerTests(StandaloneRunTestCase):
    """The standalone marker is added once the issue is marked agent working, and only read by humans."""

    def record_standalone_edits(self):
        """Record each label edit on the issue as (added, removed) from now on."""
        self.standalone_edits = []
        self.github.after_label_edit = lambda number, add, remove: (
            self.standalone_edits.append((add, sorted(remove))) if number == STANDALONE else None
        )

    def test_marker_is_added_right_after_the_issue_is_marked_agent_working(self):
        self.record_standalone_edits()

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_standalone_implemented(result, sessions=1)
        self.assertEqual(
            self.standalone_edits,
            [
                ("agent-working", ["ready-for-agent"]),
                ("standalone", []),
                ("implemented", ["agent-working"]),
            ],
        )

    def test_marker_added_by_hand_changes_nothing_about_the_run(self):
        self.new_child_run(issue_labels=("ready-for-agent", "standalone"))
        self.record_standalone_edits()

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assert_standalone_implemented(result, sessions=1)
        self.assertEqual(
            self.standalone_edits,
            [("agent-working", ["ready-for-agent"]), ("implemented", ["agent-working"])],
        )

    def test_marker_added_by_hand_does_not_make_an_unready_issue_runnable(self):
        self.new_child_run(issue_labels=("needs-info", "standalone"))

        result = self.start(loop_core.STANDARD_RUN)

        self.assert_refused(
            result,
            f"standalone issue #{STANDALONE} is not ready for an agent: labelled needs-info",
            ("needs-info", "standalone"),
        )

    def test_marker_removed_by_hand_during_the_cycle_is_not_restored(self):
        self.before_session = lambda request: self.github.edit_issue_labels(
            STANDALONE, add="agent-working", remove=["standalone"]
        )

        result = self.start(loop_core.REVIEWED_RUN, *passing_child_results(loop_core.REVIEWED_RUN))

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(len(self.sessions.requests), 2)
        self.assertEqual(self.github.labels(STANDALONE), {"implemented"})

    def test_failed_marker_write_warns_and_the_run_continues(self):
        # The marker write is the next label edit after the issue is marked agent working.
        def fail_the_marker_write(number, add, remove):
            if number == STANDALONE and add == "agent-working":
                self.github.label_edit_error = MacatiaError("gh issue edit failed: HTTP 502")

        self.github.after_label_edit = fail_the_marker_write

        result = self.start(loop_core.STANDARD_RUN, *passing_child_results(loop_core.STANDARD_RUN))

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(self.github.labels(STANDALONE), {"implemented"})
        warning = (
            f"Macatia could not label standalone issue #{STANDALONE} standalone: "
            "gh issue edit failed: HTTP 502."
        )
        self.assertIn(f"⚠️  {warning}", result.stdout)
        summary = self.github.comments(STANDALONE)[-1]
        self.assertIn("- final outcome: completed", summary)
        for printed in (summary, result.stdout.split("🏁 Macatia summary")[-1]):
            self.assertIn(f"- warning: {warning}", printed)


class StandaloneChildSessionLimitTests(StandaloneRunTestCase):
    def test_default_child_session_limit_of_a_standard_run_is_five_sessions(self):
        self.new_child_run(local_gate_results=("fail", "fail", "fail", "pass"), backup=True)

        result = self.start_engine(
            loop_core.STANDARD_RUN,
            EngineResult(writes_result=False, commit=False),
            *[EngineResult("committed")] * 4,
        )

        self.assert_standalone_implemented(result, sessions=5)
        self.assertEqual(self.engine.engines(), ["codex", "claude", "claude", "claude", "claude"])
        # The shared context names the issue and no parent.
        self.assertIn(f"Issue: https://github.com/lmpaul/buyr/issues/{STANDALONE}\n", self.engine.calls[0].prompt)
        self.assertNotIn("Parent: ", self.engine.calls[0].prompt)

    def test_default_child_session_limit_of_a_reviewed_run_is_nine_sessions(self):
        self.new_child_run(backup=True)
        findings = EngineResult("needs_correction", findings=("Handle empty carts.",))

        result = self.start_engine(
            loop_core.REVIEWED_RUN,
            EngineResult(writes_result=False, commit=False),
            *[EngineResult("committed"), findings] * 3,
            EngineResult("committed"),
            EngineResult("no_changes"),
        )

        self.assert_standalone_implemented(result, sessions=9)

    def test_child_session_limit_applies_unchanged(self):
        set_child_session_limit(self.repository, reviewed=2)

        result = self.start(
            loop_core.REVIEWED_RUN,
            ScriptedResult("committed"),
            ScriptedResult("needs_correction", findings=("Handle empty carts.",)),
        )

        self.assertEqual(result.exit_code, 1)
        self.assertIn("child session limit exhausted before issue correction", result.stderr)
        self.assertEqual(len(self.sessions.requests), 2)
        self.assertEqual(self.github.labels(STANDALONE), {"human-review", "standalone"})


class StandaloneRunBranchTests(BaseBranchTestCase):
    def setUp(self):
        super().setUp()
        self.github.children.clear()

    def test_base_branch_offers_the_issue_run_branch(self):
        confirm = ScriptedConfirm(CREATE)

        result = self.start(loop_core.STANDARD_RUN, ScriptedResult("committed"), confirm=confirm)

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(confirm.questions, [f"Create macatia/issue-{STANDALONE} from an up-to-date main?"])
        self.assertEqual(self.current_branch(), f"macatia/issue-{STANDALONE}")
        self.assertEqual(self.github.labels(STANDALONE), {"implemented", "standalone"})

    def test_refused_issue_is_refused_before_the_run_branch_is_offered(self):
        self.github.edit_issue_labels(STANDALONE, add="needs-info", remove=["ready-for-agent"])
        confirm = ScriptedConfirm(CREATE)

        result = self.start(loop_core.STANDARD_RUN, confirm=confirm)

        self.assertEqual(result.exit_code, 1)
        self.assertIn("is not ready for an agent: labelled needs-info", result.stderr)
        self.assertEqual(confirm.questions, [])
        self.assertEqual(self.current_branch(), "main")


class ScriptedChoice:
    """Picks among the startable run targets as the person at the terminal would.

    `answer` is the issue number picked, or an exception the picker raises. `offered`
    keeps the entries shown each time the picker opened, and `preselected` its preselection.
    """

    def __init__(self, answer):
        self.answer = answer
        self.offered = []
        self.preselected = []

    def __call__(self, targets, preselected):
        self.offered.append([target.label for target in targets])
        self.preselected.append(preselected)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


DEADLOCKED_PARENT = 207
UNTRUSTED_AUTHOR = "someone-else"


class RunTargetPickerTests(RunLevelTestCase):
    """A run started without a run target at a terminal offers the startable run targets."""

    def start_without_run_target(self, choose, *results, flags=(), confirm=None):
        self.sessions = ScriptedSessions(self.repository, *results)
        self.sessions.before_session = self.before_session
        return start_run(
            self.repository, self.github, self.sessions, *flags,
            confirm=confirm, choose_run_target=choose, terminal=True,
        )

    def add_deadlocked_parent(self, parent, first, second):
        """A parent whose two ready children each wait on the other."""
        self.github.add_issue(parent)
        self.github.add_issue(first, parent=parent, blocked_by=[second])
        self.github.add_issue(second, parent=parent, blocked_by=[first])

    def assert_nothing_changed(self, result):
        self.assertEqual(self.sessions.requests, [])
        self.assertEqual(self.github.comments(PARENT), [])
        self.assertEqual(self.github.labels(PARENT), {"ready-for-agent"})
        self.assertIsNone(self.github.pull_request())
        with self.assertRaises(subprocess.CalledProcessError):
            self.repository.pushed_head()

    def test_offers_only_the_startable_run_targets_by_ascending_number(self):
        github = self.github
        github.add_issue(215, labels=("agent-working",))
        github.add_issue(CHILD, parent=PARENT)
        github.add_issue(201, author=UNTRUSTED_AUTHOR)
        github.add_issue(202, state="closed")
        github.add_issue(203, labels=("needs-info",))
        github.add_issue(204, parent=203)
        github.add_issue(205)
        github.add_issue(206, parent=205, labels=("implemented",))
        github.add_issue(216, parent=205, state="closed", labels=())
        self.add_deadlocked_parent(DEADLOCKED_PARENT, 208, 209)
        github.add_issue(210)
        github.add_issue(211, parent=210, labels=("agent-working",))
        github.add_issue(212, labels=("ready-for-human",))
        github.add_issue(213, blocked_by=[214])
        github.add_issue(214)
        github.add_issue(217, blocked_by=[202])
        github.add_issue(200)
        github.issues[200]["title"] = "Add a cart"
        choose = ScriptedChoice(KeyboardInterrupt())

        self.start_without_run_target(choose)

        self.assertEqual(choose.offered, [[
            f"#{PARENT}  Issue {PARENT}  (parent · 1 ready children)",
            "#200  Add a cart  (standalone)",
            f"#{DEADLOCKED_PARENT}  Issue {DEADLOCKED_PARENT}  (parent · 2 ready children)",
            "#210  Issue 210  (parent · 0 ready children)",
            "#214  Issue 214  (standalone)",
            "#215  Issue 215  (standalone)",
            "#217  Issue 217  (standalone)",
        ]])
        self.assertEqual(choose.preselected, [None])

    def test_preselects_the_issue_of_the_run_branch_when_it_is_listed(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.add_issue(200)
        self.github.add_issue(202, state="closed")
        self.github.add_issue(203, labels=("needs-info",))
        for branch, preselected in (
            ("macatia/issue-200", 200),
            (RUN_BRANCH, PARENT),
            ("macatia/issue-202", None),
            ("macatia/issue-203", None),
            ("macatia/issue-999", None),
            ("macatia/issue-0200", None),
            ("codex/issue-200", None),
        ):
            with self.subTest(branch=branch):
                git(self.repository.path, "checkout", "-q", "-B", branch)
                choose = ScriptedChoice(KeyboardInterrupt())

                result = self.start_without_run_target(choose)

                self.assertEqual(choose.offered, [[
                    f"#{PARENT}  Issue {PARENT}  (parent · 1 ready children)",
                    "#200  Issue 200  (standalone)",
                ]])
                self.assertEqual(choose.preselected, [preselected])
                # The preselected issue still waits for the user's choice.
                self.assertEqual(result.exit_code, 1)
                self.assertIn("Macatia stopped: no run target was chosen", result.stderr)
                self.assert_nothing_changed(result)
                self.assertEqual(self.github.labels(200), {"ready-for-agent"})

    def test_picking_an_issue_runs_it_as_if_it_was_passed(self):
        git(self.repository.path, "checkout", "main")
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.add_issue(200, labels=("ready-for-human",))
        self.ready_for_agent_during_the_run(READY_FOR_HUMAN_CHILD)
        confirm = ScriptedConfirm(CREATE)
        choose = ScriptedChoice(PARENT)

        result = self.start_without_run_target(
            choose, *worst_case_child_results(loop_core.STANDARD_RUN),
            ScriptedResult("blocked", "Needs a product decision."),
            flags=("--no-caffeinate",), confirm=confirm,
        )

        self.assertEqual(choose.offered, [[f"#{PARENT}  Issue {PARENT}  (parent · 1 ready children)"]])
        self.assertEqual(confirm.questions, [f"Create {RUN_BRANCH} from an up-to-date main?"])
        self.assertEqual(git(self.repository.path, "branch", "--show-current"), RUN_BRANCH)
        self.assertEqual(result.exit_code, 1)
        # Each child gets its own child session limit, as for `macatia run 191`.
        self.assertEqual(self.session_children(), [CHILD] * 4 + [READY_FOR_HUMAN_CHILD])
        self.assertIn(
            f"and run `macatia run --no-caffeinate {PARENT}` again.",
            self.github.comments(PARENT)[-1],
        )
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_a_parent_in_dependency_deadlock_is_offered_then_reported_as_a_deadlock(self):
        reported = {}
        for name in ("explicit", "picked"):
            with self.subTest(name):
                self.github = FakeGitHub()
                self.add_deadlocked_parent(DEADLOCKED_PARENT, 208, 209)
                choose = ScriptedChoice(DEADLOCKED_PARENT)
                self.sessions = ScriptedSessions(self.repository)

                result = start_run(
                    self.repository, self.github, self.sessions,
                    *([DEADLOCKED_PARENT] if name == "explicit" else []),
                    choose_run_target=choose, terminal=True,
                )

                self.assertEqual(result.exit_code, 1)
                self.assertIn("Stop reason: parent issue has a dependency deadlock", result.stderr)
                self.assertEqual(self.sessions.requests, [])
                reported[name] = (
                    result.stderr[result.stderr.index("Stop reason:"):],
                    {child: self.github.labels(child) for child in (208, 209)},
                )
                if name == "picked":
                    self.assertEqual(choose.offered, [[
                        f"#{DEADLOCKED_PARENT}  Issue {DEADLOCKED_PARENT}  (parent · 2 ready children)"
                    ]])
        self.assertEqual(reported["picked"], reported["explicit"])

    def test_ctrl_c_in_the_picker_stops_with_nothing_changed(self):
        self.github.add_issue(CHILD, parent=PARENT)
        choose = ScriptedChoice(KeyboardInterrupt())

        result = self.start_without_run_target(choose)

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Macatia stopped: no run target was chosen", result.stderr)
        self.assertEqual(len(choose.offered), 1)
        self.assert_nothing_changed(result)
        self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})

    def test_without_a_startable_run_target_the_run_says_so_without_the_picker(self):
        # The parent has nothing left to do, and the standalone issue is held back by triage.
        self.github.add_issue(CHILD, parent=PARENT, state="closed", labels=())
        self.github.add_issue(200, labels=("needs-info",))
        choose = ScriptedChoice(PARENT)

        result = self.start_without_run_target(choose)

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertIn(f"No run target in {REPOSITORY} is ready for an agent.", result.stdout)
        self.assertEqual(choose.offered, [])
        self.assert_nothing_changed(result)

    def test_a_github_failure_during_the_listing_stops_the_run_with_its_reason(self):
        for name, fail, reason in (
            ("listing", lambda: setattr(
                self.github, "open_issues_error", MacatiaError("command failed (gh api): HTTP 502")
            ), "command failed (gh api): HTTP 502"),
            ("parent lookup", lambda: self.github.parent_error.update(
                {PARENT: MacatiaError("command failed (gh api parent): HTTP 500")}
            ), "command failed (gh api parent): HTTP 500"),
        ):
            with self.subTest(name):
                self.new_run()
                self.github.add_issue(CHILD, parent=PARENT)
                fail()
                choose = ScriptedChoice(PARENT)

                result = self.start_without_run_target(choose)

                self.assertEqual(result.exit_code, 1)
                self.assertIn(f"Macatia stopped: {reason}", result.stderr)
                self.assertEqual(choose.offered, [])
                self.assert_nothing_changed(result)

    def test_an_explicit_run_target_never_opens_the_picker(self):
        self.github.add_issue(CHILD, parent=PARENT)
        for issue, reason in (
            (CHILD, f"issue #{CHILD} is a sub-issue of #{PARENT}: run #{PARENT} instead"),
            (PARENT, None),
        ):
            with self.subTest(issue=issue):
                choose = ScriptedChoice(PARENT)
                self.sessions = ScriptedSessions(
                    self.repository, *passing_child_results(loop_core.STANDARD_RUN)
                )

                result = start_run(
                    self.repository, self.github, self.sessions, issue,
                    choose_run_target=choose, terminal=True,
                )

                self.assertEqual(choose.offered, [])
                if reason is None:
                    self.assert_completed(result, sessions=1)
                else:
                    self.assertEqual(result.exit_code, 1)
                    self.assertIn(f"Macatia stopped: {reason}", result.stderr)


PERSISTENT_TERMINAL = f"macatia-buyr-{PARENT}"


class PersistentTerminalTests(BaseBranchTestCase):
    """`--tmux` offers to restart the run in a persistent terminal once the run target's
    startup checks pass, before the run branch offer."""

    def start_with(
        self, *arguments, continuation, confirm=None, choose=None, results=(),
        existing_persistent_terminals=(),
    ):
        self.sessions = ScriptedSessions(self.repository, *results)
        return start_run(
            self.repository, self.github, self.sessions, *arguments,
            confirm=confirm, choose_run_target=choose, choose_continuation=continuation,
            terminal=True,
            existing_persistent_terminals=existing_persistent_terminals,
        )

    def assert_restarted(self, result, command):
        """The run asked once to restart as `command` in the repository root, with the sleep
        lock released, and changed nothing itself."""
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(
            result.persistent_terminal.restarts,
            [Restart(PERSISTENT_TERMINAL, command, self.repository.path.resolve(), sleep_lock_held=False)],
        )
        self.assertEqual(self.sessions.requests, [])
        self.assertEqual(self.github.comments(PARENT), [])
        self.assertEqual(self.github.labels(PARENT), {"ready-for-agent"})
        self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})
        self.assertNotIn("final outcome", result.stdout + result.stderr)
        self.assertEqual(self.current_branch(), "main")
        self.assertNotIn(RUN_BRANCH, self.local_branches())

    def test_without_the_flag_there_is_no_question_and_no_restart(self):
        continuation = ScriptedContinuation(Continuation.RESTART)
        confirm = ScriptedConfirm(CREATE)

        result = self.start_with(
            PARENT, continuation=continuation, confirm=confirm,
            results=[ScriptedResult("committed")],
        )

        self.assert_completed(result, sessions=1)
        self.assertEqual(continuation.asked, [])
        self.assertEqual(result.persistent_terminal.restarts, [])
        self.assertEqual(len(confirm.questions), 1)

    def test_restart_restarts_the_run_with_its_flags_but_tmux_in_any_position(self):
        for arguments, command in (
            (("--tmux", PARENT), f"macatia run {PARENT}"),
            ((PARENT, "--tmux"), f"macatia run {PARENT}"),
            (("--review", "--tmux", "--caffeinate", PARENT),
             f"macatia run --review --caffeinate {PARENT}"),
            ((PARENT, "--no-review", "--no-caffeinate", "--tmux"),
             f"macatia run --no-review --no-caffeinate {PARENT}"),
        ):
            with self.subTest(arguments=arguments):
                self.setUp()
                continuation = ScriptedContinuation(Continuation.RESTART)
                confirm = ScriptedConfirm(CREATE)

                result = self.start_with(*arguments, continuation=continuation, confirm=confirm)

                self.assert_restarted(result, command)
                self.assertEqual(continuation.asked, [PERSISTENT_TERMINAL])
                self.assertEqual(confirm.questions, [])

    def test_restart_keeps_the_configuration_given_by_config(self):
        (self.repository.path / "config").mkdir()
        git(self.repository.path, "mv", "macatia.toml", "config/macatia.toml")
        git(self.repository.path, "commit", "-m", "Move the Macatia configuration")
        self.base_head = self.repository.head()

        result = self.start_with(
            "--config", "config/macatia.toml", "--tmux", PARENT,
            continuation=ScriptedContinuation(Continuation.RESTART),
        )

        self.assert_restarted(result, f"macatia run --config config/macatia.toml {PARENT}")

    def test_restart_releases_the_sleep_lock_first(self):
        result = self.start_with(
            "--tmux", "--caffeinate", PARENT,
            continuation=ScriptedContinuation(Continuation.RESTART),
        )

        self.assert_restarted(result, f"macatia run --caffeinate {PARENT}")
        self.assertEqual(result.sleep_lock.taken, 1)

    def test_the_question_comes_after_the_startup_checks_and_before_the_run_branch_offer(self):
        continuation = ScriptedContinuation(Continuation.CONTINUE_HERE)
        confirm = ScriptedConfirm(CREATE)
        seen_at_question = []
        continuation.before_answer = lambda: seen_at_question.append(
            (list(confirm.questions), self.github.labels(PARENT), self.current_branch())
        )

        result = self.start_with(
            "--tmux", PARENT, continuation=continuation, confirm=confirm,
            results=[ScriptedResult("committed")],
        )

        self.assert_completed(result, sessions=1)
        self.assertEqual(seen_at_question, [([], {"ready-for-agent"}, "main")])

    def test_continue_here_runs_as_without_the_flag(self):
        continuation = ScriptedContinuation(Continuation.CONTINUE_HERE)
        confirm = ScriptedConfirm(CREATE)

        result = self.start_with(
            PARENT, "--tmux", continuation=continuation, confirm=confirm,
            results=[ScriptedResult("committed")],
        )

        self.assert_completed(result, sessions=1)
        self.assertEqual(continuation.asked, [PERSISTENT_TERMINAL])
        self.assertEqual(confirm.questions, [f"Create {RUN_BRANCH} from an up-to-date main?"])
        self.assertEqual(result.persistent_terminal.restarts, [])
        self.assertEqual(self.current_branch(), RUN_BRANCH)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_rerun_command_of_a_run_continued_here_leaves_out_the_flag(self):
        result = self.start_with(
            "--tmux", PARENT, "--caffeinate",
            continuation=ScriptedContinuation(Continuation.CONTINUE_HERE),
            confirm=ScriptedConfirm(CREATE), results=[ScriptedResult("blocked", "Stuck.")],
        )

        self.assertEqual(result.exit_code, 1)
        self.assertIn(f"run `macatia run --caffeinate {PARENT}` again", result.stderr)
        self.assertNotIn("--tmux", result.stderr)

    def test_stop_and_ctrl_c_change_nothing(self):
        for answer in (Continuation.STOP, KeyboardInterrupt()):
            with self.subTest(answer=answer):
                confirm = ScriptedConfirm(CREATE)

                result = self.start_with(
                    "--tmux", PARENT, continuation=ScriptedContinuation(answer), confirm=confirm,
                )

                self.assert_nothing_changed(result, f"Macatia stopped: {PERSISTENT_TERMINAL} was not started")
                self.assertEqual(result.exit_code, 1)
                self.assertEqual(confirm.questions, [])
                self.assertEqual(result.persistent_terminal.restarts, [])

    def test_refused_run_target_stops_before_the_question(self):
        self.github.edit_issue_labels(PARENT, "needs-info", ())
        continuation = ScriptedContinuation(Continuation.RESTART)

        result = self.start_with("--tmux", PARENT, continuation=continuation)

        self.assert_nothing_changed(result, "held back by triage: needs-info")
        self.assertEqual(continuation.asked, [])
        self.assertEqual(result.persistent_terminal.restarts, [])

    def test_refused_standalone_issue_stops_before_the_question(self):
        self.github.children.clear()
        self.github.edit_issue_labels(PARENT, add="needs-info", remove=["ready-for-agent"])
        continuation = ScriptedContinuation(Continuation.RESTART)

        result = self.start_with("--tmux", PARENT, continuation=continuation)

        self.assertEqual(result.exit_code, 1)
        self.assertIn("is not ready for an agent: labelled needs-info", result.stderr)
        self.assertEqual(continuation.asked, [])
        self.assertEqual(result.persistent_terminal.restarts, [])

    def test_standalone_issue_restarts_once_its_startup_checks_pass(self):
        self.github.children.clear()

        result = self.start_with(
            "--tmux", PARENT, continuation=ScriptedContinuation(Continuation.RESTART),
        )

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(
            [restart.name for restart in result.persistent_terminal.restarts],
            [PERSISTENT_TERMINAL],
        )
        self.assertEqual(self.github.labels(PARENT), {"ready-for-agent"})

    def test_an_existing_persistent_terminal_stops_the_run_before_the_question(self):
        continuation = ScriptedContinuation(Continuation.RESTART)
        confirm = ScriptedConfirm(CREATE)

        result = self.start_with(
            "--tmux", "--caffeinate", PARENT, continuation=continuation, confirm=confirm,
            existing_persistent_terminals=[PERSISTENT_TERMINAL],
        )

        self.assert_nothing_changed(
            result,
            f"{PERSISTENT_TERMINAL} already exists: attach to it with "
            f"tmux attach -t {PERSISTENT_TERMINAL}, or end it first",
        )
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(continuation.asked, [])
        self.assertEqual(confirm.questions, [])
        self.assertEqual(result.persistent_terminal.restarts, [])
        self.assertEqual(self.github.labels(PARENT), {"ready-for-agent"})
        self.assertFalse(result.sleep_lock.held)

    def test_a_refused_run_target_stops_before_the_existing_persistent_terminal_is_reported(self):
        self.github.edit_issue_labels(PARENT, "needs-info", ())

        result = self.start_with(
            "--tmux", PARENT, continuation=ScriptedContinuation(Continuation.RESTART),
            existing_persistent_terminals=[PERSISTENT_TERMINAL],
        )

        self.assert_nothing_changed(result, "held back by triage: needs-info")
        self.assertNotIn("already exists", result.stderr)

    def test_a_persistent_terminal_of_another_name_does_not_stop_the_run(self):
        result = self.start_with(
            "--tmux", PARENT, continuation=ScriptedContinuation(Continuation.RESTART),
            existing_persistent_terminals=[f"macatia-other-{PARENT}", "macatia-buyr-200", PERSISTENT_TERMINAL[:-1]],
        )

        self.assert_restarted(result, f"macatia run {PARENT}")

    def test_without_a_run_target_the_picked_one_names_the_persistent_terminal_and_restarts(self):
        self.github.add_issue(200)
        continuation = ScriptedContinuation(Continuation.RESTART)
        choose = ScriptedChoice(PARENT)
        picked_before_question = []
        continuation.before_answer = lambda: picked_before_question.append(len(choose.offered))

        result = self.start_with(
            "--tmux", "--review", continuation=continuation, choose=choose,
        )

        self.assert_restarted(result, f"macatia run --review {PARENT}")
        self.assertEqual(picked_before_question, [1])

    def test_an_unavailable_persistent_terminal_stops_before_the_configuration_and_github(self):
        tmux_not_found = "tmux not found: install tmux, or run without --tmux"
        inside_tmux = "already inside tmux: run without --tmux"
        needs_a_terminal = "--tmux needs a terminal: run in a terminal, or without --tmux"
        for unavailability, terminal, error in (
            (Unavailability.TMUX_NOT_FOUND, True, tmux_not_found),
            (Unavailability.INSIDE_TMUX, True, inside_tmux),
            (None, False, needs_a_terminal),
        ):
            with self.subTest(error=error):
                self.setUp()
                self.sessions = ScriptedSessions(self.repository)
                # Outside a terminal there is neither picker nor continuation choice.
                choose = ScriptedChoice(PARENT) if terminal else None
                continuation = ScriptedContinuation(Continuation.RESTART) if terminal else None
                issue = () if terminal else (PARENT,)

                # A missing configuration shows it is never read.
                result = start_run(
                    self.repository, self.github, self.sessions,
                    "--tmux", "--caffeinate", "--config", "missing.toml", *issue,
                    choose_run_target=choose, choose_continuation=continuation,
                    persistent_terminal_unavailability=unavailability,
                )

                self.assertEqual(result.exit_code, 1)
                self.assertEqual(result.stderr.count(error), 1, result.stderr)
                self.assertNotIn("missing.toml", result.stderr)
                self.assertEqual(self.github.opened_for, [])
                self.assertEqual(result.sleep_lock.taken, 0)
                self.assertEqual(result.persistent_terminal.restarts, [])
                if terminal:
                    self.assertEqual(choose.offered, [])
                    self.assertEqual(continuation.asked, [])

    def test_without_the_flag_the_persistent_terminal_is_never_checked(self):
        for unavailability in Unavailability:
            with self.subTest(unavailability=unavailability):
                self.setUp()
                self.sessions = ScriptedSessions(self.repository, ScriptedResult("committed"))

                result = start_run(
                    self.repository, self.github, self.sessions, PARENT,
                    confirm=ScriptedConfirm(CREATE), choose_continuation=None,
                    persistent_terminal_unavailability=unavailability,
                )

                self.assert_completed(result, sessions=1)
                self.assertEqual(result.persistent_terminal.checked, 0)


class RunWorktreeTestCase(BaseBranchTestCase):
    """A `--worktree` run started from the person's checkout."""

    def setUp(self):
        super().setUp()
        self.run_worktree = (
            self.repository.path.parent / "repository.worktrees" / f"issue-{PARENT}"
        ).resolve()

    def start_with(self, *arguments, confirm=None, choose=None, continuation=None, results=(), cwd=None):
        self.sessions = ScriptedSessions(self.repository, *results)
        return start_run(
            self.repository, self.github, self.sessions, *arguments,
            confirm=confirm, choose_run_target=choose, choose_continuation=continuation,
            terminal=confirm is not None, cwd=cwd,
        )

    def persons_checkout(self):
        """The person works on a branch of their own with uncommitted changes."""
        git(self.repository.path, "switch", "-q", "--create", "feature")
        (self.repository.path / "draft.txt").write_text("draft\n", encoding="utf-8")

    def assert_checkout_untouched(self):
        self.assertEqual(self.current_branch(), "feature")
        self.assertEqual(
            git(self.repository.path, "status", "--porcelain"), "?? draft.txt"
        )
        self.assertEqual(git(self.repository.path, "rev-parse", "main"), self.base_head)

    def assert_nothing_created(self, result, reason):
        self.assertEqual(result.exit_code, 1)
        self.assertIn(reason, result.stderr)
        self.assertEqual(result.persistent_terminal.restarts, [])
        self.assertFalse(self.run_worktree.exists())
        self.assertNotIn(RUN_BRANCH, self.local_branches())
        self.assertNotIn(RUN_BRANCH, self.origin_branches())
        self.assertEqual(self.sessions.requests, [])
        self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})

    def push_to_origin_run_branch(self, name):
        """Another clone pushes a commit to `origin`'s run branch; returns its sha."""
        clone = self.repository.path.parent / f"other-run-clone-{name}"
        git(self.repository.path.parent, "clone", "-q", "-b", RUN_BRANCH, str(self.repository.origin), str(clone))
        git(clone, "config", "user.email", "other@example.com")
        git(clone, "config", "user.name", "Other")
        (clone / name).write_text(f"{name}\n", encoding="utf-8")
        git(clone, "add", name)
        git(clone, "commit", "-q", "-m", f"Add {name}")
        git(clone, "push", "-q", "origin", RUN_BRANCH)
        return git(clone, "rev-parse", "HEAD")

    def start_inside(self, run_worktree, *arguments, confirm, continuation=None, results=()):
        """Start a run with `arguments` from inside `run_worktree`, as a terminal would."""
        in_run_worktree = copy.copy(self.repository)
        in_run_worktree.path = run_worktree
        self.sessions = ScriptedSessions(in_run_worktree, *results)
        return start_run(
            in_run_worktree, self.github, self.sessions, *arguments,
            confirm=confirm, choose_continuation=continuation, terminal=True, cwd=run_worktree,
        )


class RunWorktreeTests(RunWorktreeTestCase):
    """`--worktree` offers, in place of the run branch offer, to create the run worktree of
    the run target, then restarts the run in it, leaving the person's checkout untouched."""

    def test_create_adds_the_run_worktree_from_the_remote_base_and_restarts_in_it(self):
        remote_head = self.push_to_origin_main("upstream.txt")
        self.persons_checkout()
        confirm = ScriptedConfirm(CREATE)

        result = self.start_with("--worktree", PARENT, confirm=confirm)

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(
            confirm.questions, [f"Create a run worktree for {RUN_BRANCH} at {self.run_worktree}?"]
        )
        self.assertEqual(confirm.choices, [("Create", True)])
        self.assertEqual(
            result.persistent_terminal.restarts,
            [Restart(None, f"macatia run --worktree {PARENT}", self.run_worktree, sleep_lock_held=False)],
        )
        self.assertEqual(git(self.run_worktree, "branch", "--show-current"), RUN_BRANCH)
        self.assertEqual(git(self.run_worktree, "rev-parse", "HEAD"), remote_head)
        self.assertEqual(git(self.run_worktree, "status", "--porcelain"), "")
        self.assert_checkout_untouched()
        self.assertEqual(self.sessions.requests, [])
        self.assertEqual(self.github.comments(PARENT), [])
        self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})

    def test_restart_keeps_the_flags_but_tmux_in_any_position(self):
        (self.repository.path / "config").mkdir()
        git(self.repository.path, "mv", "macatia.toml", "config/macatia.toml")
        git(self.repository.path, "commit", "-q", "-m", "Move the Macatia configuration")
        git(self.repository.path, "push", "-q", "origin", "main")
        self.base_head = self.repository.head()
        for arguments, command in (
            ((PARENT, "--worktree", "--config", "config/macatia.toml"),
             f"macatia run --worktree --config config/macatia.toml {PARENT}"),
            (("--review", "--config", "config/macatia.toml", "--caffeinate", PARENT, "--worktree"),
             f"macatia run --review --caffeinate --worktree --config config/macatia.toml {PARENT}"),
            (("--worktree", "--no-review", "--no-caffeinate", "--config", "config/macatia.toml", PARENT),
             f"macatia run --no-review --no-caffeinate --worktree --config config/macatia.toml {PARENT}"),
        ):
            with self.subTest(arguments=arguments):
                if self.run_worktree.exists():
                    git(self.repository.path, "worktree", "remove", str(self.run_worktree))
                    git(self.repository.path, "branch", "-q", "-D", RUN_BRANCH)

                result = self.start_with(*arguments, confirm=ScriptedConfirm(CREATE))

                self.assertEqual(result.exit_code, 0, result.stderr)
                self.assertEqual(
                    result.persistent_terminal.restarts,
                    [Restart(None, command, self.run_worktree, sleep_lock_held=False)],
                )

    def test_restart_reads_an_absolute_configuration_in_the_run_worktree(self):
        configuration = self.repository.path.resolve() / "macatia.toml"

        result = self.start_with("--worktree", "--config", configuration, PARENT, confirm=ScriptedConfirm(CREATE))

        self.assertEqual(
            result.persistent_terminal.restarts,
            [Restart(None, f"macatia run --worktree {PARENT}", self.run_worktree, sleep_lock_held=False)],
        )

    def test_restart_releases_the_sleep_lock_first(self):
        result = self.start_with("--worktree", "--caffeinate", PARENT, confirm=ScriptedConfirm(CREATE))

        self.assertEqual(
            result.persistent_terminal.restarts,
            [Restart(None, f"macatia run --caffeinate --worktree {PARENT}", self.run_worktree,
                     sleep_lock_held=False)],
        )
        self.assertEqual(result.sleep_lock.taken, 1)

    def test_stop_and_ctrl_c_change_nothing(self):
        self.persons_checkout()
        for answer in (STOP, KeyboardInterrupt()):
            with self.subTest(answer=answer):
                confirm = ScriptedConfirm(answer)

                result = self.start_with("--worktree", PARENT, confirm=confirm)

                self.assert_nothing_created(
                    result, f"the run worktree for {RUN_BRANCH} was not created"
                )
                self.assertEqual(len(confirm.questions), 1)
                self.assert_checkout_untouched()

    def test_without_a_terminal_the_run_stops_and_names_the_run_worktree_it_would_create(self):
        self.persons_checkout()

        result = self.start_with("--worktree", PARENT)

        self.assert_nothing_created(
            result,
            f"{RUN_BRANCH} has no run worktree: run `macatia run --worktree {PARENT}` "
            f"in a terminal to create it at {self.run_worktree}",
        )
        self.assert_checkout_untouched()

    def test_a_configuration_missing_from_the_remote_base_stops_before_the_question(self):
        self.persons_checkout()
        shutil.copy(self.repository.path / "macatia.toml", self.repository.path / "local.toml")
        confirm = ScriptedConfirm(CREATE)

        result = self.start_with("--worktree", "--config", "local.toml", PARENT, confirm=confirm)

        self.assert_nothing_created(
            result,
            f"{self.repository.path.resolve() / 'local.toml'} is not on origin/main, where the "
            "run worktree reads it: commit and push it, or run without --worktree",
        )
        self.assertEqual(confirm.questions, [])

    def test_without_a_run_target_the_picker_comes_first_then_the_offer(self):
        self.persons_checkout()
        self.github.add_issue(200)
        choose = ScriptedChoice(PARENT)
        confirm = ScriptedConfirm(CREATE)
        picked_before_question = []

        def answer(question, accept, *, accept_by_default):
            picked_before_question.append(len(choose.offered))
            return confirm(question, accept, accept_by_default=accept_by_default)

        result = self.start_with("--worktree", choose=choose, confirm=answer)

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(picked_before_question, [1])
        self.assertEqual(
            result.persistent_terminal.restarts,
            [Restart(None, f"macatia run --worktree {PARENT}", self.run_worktree, sleep_lock_held=False)],
        )
        self.assert_checkout_untouched()

    def restart_and_run_there(self, *results):
        """Create the run worktree from the person's checkout, then start the restarted command
        where it was asked to run, as the restart would; returns that run's result."""
        self.persons_checkout()
        restarted = self.start_with("--worktree", PARENT, confirm=ScriptedConfirm(CREATE))
        [restart] = restarted.persistent_terminal.restarts
        self.confirm = ScriptedConfirm(CREATE)
        return self.start_inside(
            restart.directory, *shlex.split(restart.command)[2:], confirm=self.confirm, results=results
        )

    def test_the_restarted_command_runs_in_the_run_worktree(self):
        result = self.restart_and_run_there(ScriptedResult("committed"))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.confirm.questions, [])
        self.assertEqual(result.persistent_terminal.restarts, [])
        self.assertEqual(self.repository.pushed_head(RUN_BRANCH), git(self.run_worktree, "rev-parse", "HEAD"))
        self.assertTrue(any((self.run_worktree / ".macatia" / "diagnostics").iterdir()))
        self.assert_checkout_untouched()

    def test_a_stopped_runs_rerun_command_keeps_the_flag(self):
        result = self.restart_and_run_there(ScriptedResult("blocked", "Stuck."))

        self.assertEqual(result.exit_code, 1)
        self.assertIn(f"run `macatia run --worktree {PARENT}` again", result.stderr)


class RunWorktreeWithPersistentTerminalTests(RunWorktreeTestCase):
    """`--worktree --tmux` asks where the run continues after the run worktree offer, then
    restarts the run once, in the run worktree."""

    def setUp(self):
        super().setUp()
        self.persons_checkout()

    def start_with_tmux(self, *arguments, continuation, unavailability=None, existing=()):
        self.confirm = ScriptedConfirm(CREATE)
        self.sessions = ScriptedSessions(self.repository)
        # The run worktree question comes before the continuation choice.
        continuation.before_answer = lambda: self.questions_before_choice.append(
            list(self.confirm.questions)
        )
        self.questions_before_choice = []
        return start_run(
            self.repository, self.github, self.sessions, "--worktree", "--tmux", *arguments,
            confirm=self.confirm, choose_run_target=None, choose_continuation=continuation,
            terminal=True, persistent_terminal_unavailability=unavailability,
            existing_persistent_terminals=existing,
        )

    def test_restart_starts_the_persistent_terminal_in_the_run_worktree_once(self):
        continuation = ScriptedContinuation(Continuation.RESTART)

        result = self.start_with_tmux("--review", PARENT, continuation=continuation)

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(
            self.questions_before_choice,
            [[f"Create a run worktree for {RUN_BRANCH} at {self.run_worktree}?"]],
        )
        self.assertEqual(continuation.asked, [PERSISTENT_TERMINAL])
        self.assertEqual(
            result.persistent_terminal.restarts,
            [Restart(PERSISTENT_TERMINAL, f"macatia run --review --worktree {PARENT}",
                     self.run_worktree, sleep_lock_held=False)],
        )
        self.assertEqual(git(self.run_worktree, "branch", "--show-current"), RUN_BRANCH)
        self.assertEqual(self.sessions.requests, [])
        self.assert_checkout_untouched()

    def test_continue_here_restarts_in_the_run_worktree_in_this_terminal_once(self):
        result = self.start_with_tmux(PARENT, continuation=ScriptedContinuation(Continuation.CONTINUE_HERE))

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(
            result.persistent_terminal.restarts,
            [Restart(None, f"macatia run --worktree {PARENT}", self.run_worktree,
                     sleep_lock_held=False)],
        )
        self.assertEqual(self.sessions.requests, [])
        self.assert_checkout_untouched()

    def test_stop_keeps_the_run_worktree_for_the_next_run(self):
        for answer in (Continuation.STOP, KeyboardInterrupt()):
            with self.subTest(answer=answer):
                self.setUp()

                result = self.start_with_tmux(PARENT, continuation=ScriptedContinuation(answer))

                self.assertEqual(result.exit_code, 1)
                self.assertIn(f"{PERSISTENT_TERMINAL} was not started", result.stderr)
                self.assertEqual(result.persistent_terminal.restarts, [])
                self.assertEqual(git(self.run_worktree, "branch", "--show-current"), RUN_BRANCH)
                self.assertEqual(self.sessions.requests, [])
                self.assert_checkout_untouched()

                continuation = ScriptedContinuation(Continuation.RESTART)
                again = self.start_with_tmux(PARENT, continuation=continuation)

                self.assertEqual(again.exit_code, 0, again.stderr)
                self.assertEqual(
                    self.questions_before_choice,
                    [[f"Continue in the existing run worktree at {self.run_worktree}?"]],
                )
                self.assertEqual(
                    again.persistent_terminal.restarts,
                    [Restart(PERSISTENT_TERMINAL, f"macatia run --worktree {PARENT}",
                             self.run_worktree, sleep_lock_held=False)],
                )

    def test_from_inside_the_run_worktree_restart_starts_the_persistent_terminal_there(self):
        self.start_with_tmux(PARENT, continuation=ScriptedContinuation(Continuation.STOP))
        continuation = ScriptedContinuation(Continuation.RESTART)
        self.sessions = ScriptedSessions(self.repository)

        result = self.start_inside(
            self.run_worktree, "--worktree", "--tmux", PARENT, confirm=ScriptedConfirm(CREATE),
            continuation=continuation,
        )

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(continuation.asked, [PERSISTENT_TERMINAL])
        self.assertEqual(
            result.persistent_terminal.restarts,
            [Restart(PERSISTENT_TERMINAL, f"macatia run --worktree {PARENT}",
                     self.run_worktree, sleep_lock_held=False)],
        )

    def test_an_existing_persistent_terminal_stops_the_run_before_the_run_worktree_offer(self):
        continuation = ScriptedContinuation(Continuation.RESTART)

        result = self.start_with_tmux(PARENT, continuation=continuation, existing=[PERSISTENT_TERMINAL])

        self.assert_nothing_created(
            result,
            f"{PERSISTENT_TERMINAL} already exists: attach to it with "
            f"tmux attach -t {PERSISTENT_TERMINAL}, or end it first",
        )
        self.assertEqual(self.confirm.questions, [])
        self.assertEqual(continuation.asked, [])
        self.assert_checkout_untouched()

    def test_an_unavailable_persistent_terminal_stops_before_the_run_worktree_offer(self):
        for unavailability, error in (
            (Unavailability.TMUX_NOT_FOUND, "tmux not found: install tmux, or run without --tmux"),
            (Unavailability.INSIDE_TMUX, "already inside tmux: run without --tmux"),
        ):
            with self.subTest(unavailability=unavailability):
                self.setUp()

                result = self.start_with_tmux(
                    PARENT, continuation=ScriptedContinuation(Continuation.RESTART),
                    unavailability=unavailability,
                )

                self.assert_nothing_created(result, error)
                self.assertEqual(self.github.opened_for, [])
                self.assertEqual(self.confirm.questions, [])

    def test_without_a_terminal_the_run_stops_before_the_run_worktree_offer(self):
        self.sessions = ScriptedSessions(self.repository)

        result = start_run(
            self.repository, self.github, self.sessions, "--worktree", "--tmux", PARENT,
        )

        self.assert_nothing_created(
            result, "--tmux needs a terminal: run in a terminal, or without --tmux"
        )
        self.assertEqual(self.github.opened_for, [])


class ExistingRunWorktreeTests(RunWorktreeTestCase):
    """A `--worktree` run whose run worktree already exists offers to continue in it."""

    def setUp(self):
        super().setUp()
        git(self.repository.path, "worktree", "add", "-q", "-b", RUN_BRANCH, str(self.run_worktree), "main")
        self.run_worktree_head = self.commit_in_run_worktree("progress.txt")
        git(self.run_worktree, "push", "-q", "-u", "origin", RUN_BRANCH)
        self.persons_checkout()

    def commit_in_run_worktree(self, name):
        (self.run_worktree / name).write_text(f"{name}\n", encoding="utf-8")
        git(self.run_worktree, "add", name)
        git(self.run_worktree, "commit", "-q", "-m", f"Add {name}")
        return git(self.run_worktree, "rev-parse", "HEAD")

    def assert_nothing_continued(self, result, reason):
        self.assertEqual(result.exit_code, 1)
        self.assertIn(reason, result.stderr)
        self.assertEqual(result.persistent_terminal.restarts, [])
        self.assertEqual(git(self.run_worktree, "rev-parse", "HEAD"), self.run_worktree_head)
        self.assertEqual(self.sessions.requests, [])
        self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})
        self.assert_checkout_untouched()

    def test_continue_restarts_in_the_existing_run_worktree(self):
        confirm = ScriptedConfirm(RESUME)

        result = self.start_with("--worktree", PARENT, confirm=confirm)

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(
            confirm.questions, [f"Continue in the existing run worktree at {self.run_worktree}?"]
        )
        self.assertEqual(confirm.choices, [("Continue", True)])
        self.assertEqual(
            result.persistent_terminal.restarts,
            [Restart(None, f"macatia run --worktree {PARENT}", self.run_worktree, sleep_lock_held=False)],
        )
        self.assertEqual(git(self.run_worktree, "rev-parse", "HEAD"), self.run_worktree_head)
        self.assert_checkout_untouched()
        self.assertEqual(self.sessions.requests, [])

    def test_continue_fast_forwards_a_run_branch_behind_the_remote_one(self):
        remote_head = self.push_to_origin_run_branch("remote.txt")

        result = self.start_with("--worktree", PARENT, confirm=ScriptedConfirm(RESUME))

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(git(self.run_worktree, "rev-parse", "HEAD"), remote_head)
        self.assertEqual(git(self.run_worktree, "status", "--porcelain"), "")
        self.assertEqual(len(result.persistent_terminal.restarts), 1)
        self.assert_checkout_untouched()

    def test_a_diverged_run_branch_stops_with_nothing_rewritten(self):
        remote_head = self.push_to_origin_run_branch("remote.txt")
        self.run_worktree_head = self.commit_in_run_worktree("local.txt")

        result = self.start_with("--worktree", PARENT, confirm=ScriptedConfirm(RESUME))

        self.assert_nothing_continued(
            result,
            f"{RUN_BRANCH} has diverged from origin/{RUN_BRANCH}; reconcile them, "
            f"then run `macatia run --worktree {PARENT}` again",
        )
        self.assertEqual(self.repository.pushed_head(RUN_BRANCH), remote_head)

    def test_stop_and_ctrl_c_change_nothing(self):
        self.push_to_origin_run_branch("remote.txt")
        for answer in (STOP, KeyboardInterrupt()):
            with self.subTest(answer=answer):
                confirm = ScriptedConfirm(answer)

                result = self.start_with("--worktree", PARENT, confirm=confirm)

                self.assert_nothing_continued(
                    result, f"the run did not continue in the run worktree {self.run_worktree}"
                )
                self.assertEqual(len(confirm.questions), 1)

    def test_a_configuration_missing_from_the_run_branch_stops_before_the_question(self):
        self.push_to_origin_run_branch("remote.txt")
        shutil.copy(self.repository.path / "macatia.toml", self.repository.path / "local.toml")
        confirm = ScriptedConfirm(RESUME)

        result = self.start_with("--worktree", "--config", "local.toml", PARENT, confirm=confirm)

        self.assertEqual(result.exit_code, 1)
        self.assertIn(
            f"{self.repository.path.resolve() / 'local.toml'} is not on origin/{RUN_BRANCH}, "
            "where the run worktree reads it: commit and push it, or run without --worktree",
            result.stderr,
        )
        self.assertEqual(confirm.questions, [])
        self.assertEqual(result.persistent_terminal.restarts, [])
        self.assertEqual(git(self.run_worktree, "rev-parse", "HEAD"), self.run_worktree_head)

    def test_a_deleted_run_worktree_is_not_continued_in(self):
        shutil.rmtree(self.run_worktree)
        confirm = ScriptedConfirm(RESUME)

        result = self.start_with("--worktree", PARENT, confirm=confirm)

        self.assertEqual(result.exit_code, 1)
        self.assertIn(
            f"{RUN_BRANCH} is checked out in {self.run_worktree}, which no longer exists: "
            "remove it with `git worktree prune`, or run without --worktree",
            result.stderr,
        )
        self.assertEqual(confirm.questions, [])
        self.assertEqual(result.persistent_terminal.restarts, [])

    def test_without_a_terminal_the_run_stops_and_names_the_run_worktree_to_continue_in(self):
        result = self.start_with("--worktree", PARENT)

        self.assert_nothing_continued(
            result,
            f"{RUN_BRANCH} has a run worktree at {self.run_worktree}: run "
            f"`macatia run --worktree {PARENT}` in a terminal to continue in it",
        )

    def test_another_run_target_from_inside_a_run_worktree_gets_its_own_next_to_the_main_checkout(self):
        self.github.add_issue(200)
        other_run_worktree = self.run_worktree.parent / "issue-200"
        confirm = ScriptedConfirm(CREATE)

        result = self.start_inside(self.run_worktree, "--worktree", 200, confirm=confirm)

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(
            confirm.questions,
            [f"Create a run worktree for macatia/issue-200 at {other_run_worktree}?"],
        )
        self.assertEqual(
            result.persistent_terminal.restarts,
            [Restart(None, "macatia run --worktree 200", other_run_worktree, sleep_lock_held=False)],
        )
        self.assertEqual(git(self.run_worktree, "branch", "--show-current"), RUN_BRANCH)
        self.assertEqual(git(self.run_worktree, "rev-parse", "HEAD"), self.run_worktree_head)
        self.assert_checkout_untouched()


class ExistingRunBranchRunWorktreeTests(RunWorktreeTestCase):
    """A `--worktree` run whose run branch exists, checked out nowhere, offers to create the
    run worktree on it, following the remote one."""

    def setUp(self):
        super().setUp()
        git(self.repository.path, "branch", RUN_BRANCH, "main")
        self.local_head = self.commit_on_run_branch("progress.txt")
        self.persons_checkout()

    def commit_on_run_branch(self, name):
        """Commit `name` on the local run branch without checking it out; returns its sha."""
        scratch = self.repository.path.parent / f"scratch-{name}"
        git(self.repository.path, "worktree", "add", "-q", str(scratch), RUN_BRANCH)
        (scratch / name).write_text(f"{name}\n", encoding="utf-8")
        git(scratch, "add", name)
        git(scratch, "commit", "-q", "-m", f"Add {name}")
        head = git(scratch, "rev-parse", "HEAD")
        git(self.repository.path, "worktree", "remove", str(scratch))
        return head

    def assert_created_at(self, result, confirm, head):
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(
            confirm.questions, [f"Create a run worktree for {RUN_BRANCH} at {self.run_worktree}?"]
        )
        self.assertEqual(
            result.persistent_terminal.restarts,
            [Restart(None, f"macatia run --worktree {PARENT}", self.run_worktree, sleep_lock_held=False)],
        )
        self.assertEqual(git(self.run_worktree, "branch", "--show-current"), RUN_BRANCH)
        self.assertEqual(git(self.run_worktree, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.run_worktree, "status", "--porcelain"), "")
        self.assert_checkout_untouched()
        self.assertEqual(self.sessions.requests, [])

    def assert_upstream(self):
        self.assertEqual(
            git(self.run_worktree, "rev-parse", "--abbrev-ref", f"{RUN_BRANCH}@{{upstream}}"),
            f"origin/{RUN_BRANCH}",
        )

    def assert_stopped(self, result, reason):
        self.assertEqual(result.exit_code, 1)
        self.assertIn(reason, result.stderr)
        self.assertEqual(result.persistent_terminal.restarts, [])
        self.assertEqual(self.sessions.requests, [])
        self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})
        self.assert_checkout_untouched()

    def test_create_adds_the_run_worktree_on_a_local_run_branch(self):
        confirm = ScriptedConfirm(CREATE)

        result = self.start_with("--worktree", PARENT, confirm=confirm)

        self.assert_created_at(result, confirm, self.local_head)
        self.assertNotIn(RUN_BRANCH, self.origin_branches())

    def test_create_fast_forwards_a_local_run_branch_behind_the_remote_one(self):
        git(self.repository.path, "push", "-q", "origin", RUN_BRANCH)
        remote_head = self.push_to_origin_run_branch("remote.txt")
        confirm = ScriptedConfirm(CREATE)

        result = self.start_with("--worktree", PARENT, confirm=confirm)

        self.assert_created_at(result, confirm, remote_head)
        self.assert_upstream()

    def test_create_keeps_a_local_run_branch_ahead_of_the_remote_one(self):
        git(self.repository.path, "push", "-q", "origin", RUN_BRANCH)
        pushed_head = self.local_head
        local_head = self.commit_on_run_branch("local.txt")
        confirm = ScriptedConfirm(CREATE)

        result = self.start_with("--worktree", PARENT, confirm=confirm)

        self.assert_created_at(result, confirm, local_head)
        self.assert_upstream()
        self.assertEqual(self.repository.pushed_head(RUN_BRANCH), pushed_head)

    def test_create_checks_out_a_run_branch_only_on_the_remote_tracking_it(self):
        git(self.repository.path, "push", "-q", "origin", RUN_BRANCH)
        git(self.repository.path, "branch", "-q", "-D", RUN_BRANCH)
        confirm = ScriptedConfirm(CREATE)

        result = self.start_with("--worktree", PARENT, confirm=confirm)

        self.assert_created_at(result, confirm, self.local_head)
        self.assert_upstream()

    def test_a_diverged_run_branch_stops_with_nothing_rewritten(self):
        git(self.repository.path, "push", "-q", "origin", RUN_BRANCH)
        remote_head = self.push_to_origin_run_branch("remote.txt")
        local_head = self.commit_on_run_branch("local.txt")
        confirm = ScriptedConfirm(CREATE)

        result = self.start_with("--worktree", PARENT, confirm=confirm)

        self.assert_stopped(
            result,
            f"{RUN_BRANCH} has diverged from origin/{RUN_BRANCH}; reconcile them, "
            f"then run `macatia run --worktree {PARENT}` again",
        )
        self.assertEqual(confirm.questions, [])
        self.assertFalse(self.run_worktree.exists())
        self.assertEqual(git(self.repository.path, "rev-parse", RUN_BRANCH), local_head)
        self.assertEqual(self.repository.pushed_head(RUN_BRANCH), remote_head)

    def test_stop_and_ctrl_c_change_nothing(self):
        for answer in (STOP, KeyboardInterrupt()):
            with self.subTest(answer=answer):
                confirm = ScriptedConfirm(answer)

                result = self.start_with("--worktree", PARENT, confirm=confirm)

                self.assert_stopped(result, f"the run worktree for {RUN_BRANCH} was not created")
                self.assertEqual(len(confirm.questions), 1)
                self.assertFalse(self.run_worktree.exists())
                self.assertEqual(git(self.repository.path, "rev-parse", RUN_BRANCH), self.local_head)

    def test_without_a_terminal_the_run_stops_and_names_the_run_worktree_it_would_create(self):
        result = self.start_with("--worktree", PARENT)

        self.assert_stopped(
            result,
            f"{RUN_BRANCH} has no run worktree: run `macatia run --worktree {PARENT}` "
            f"in a terminal to create it at {self.run_worktree}",
        )
        self.assertFalse(self.run_worktree.exists())

    def test_the_configuration_is_required_on_the_run_branch_not_the_remote_base(self):
        # On origin/main but not on the run branch.
        git(self.repository.path, "stash", "-q", "--include-untracked")
        git(self.repository.path, "switch", "-q", "main")
        shutil.copy(self.repository.path / "macatia.toml", self.repository.path / "local.toml")
        git(self.repository.path, "add", "local.toml")
        git(self.repository.path, "commit", "-q", "-m", "Add local.toml")
        git(self.repository.path, "push", "-q", "origin", "main")
        self.base_head = self.repository.head()
        git(self.repository.path, "switch", "-q", "feature")
        git(self.repository.path, "merge", "-q", "--ff-only", "main")
        git(self.repository.path, "stash", "pop", "-q")
        confirm = ScriptedConfirm(CREATE)

        result = self.start_with("--worktree", "--config", "local.toml", PARENT, confirm=confirm)

        self.assert_stopped(
            result,
            f"{self.repository.path.resolve() / 'local.toml'} is not on {RUN_BRANCH}, where the "
            "run worktree reads it: commit and push it, or run without --worktree",
        )
        self.assertEqual(confirm.questions, [])
        self.assertFalse(self.run_worktree.exists())

    def test_a_run_branch_checked_out_in_another_checkout_stops_naming_it(self):
        elsewhere = (self.repository.path.parent / "elsewhere").resolve()
        git(self.repository.path, "worktree", "add", "-q", str(elsewhere), RUN_BRANCH)
        confirm = ScriptedConfirm(CREATE)

        result = self.start_with("--worktree", PARENT, confirm=confirm)

        self.assert_stopped(
            result,
            f"{RUN_BRANCH} is checked out in {elsewhere}: switch it to another branch there, "
            "or run without --worktree",
        )
        self.assertEqual(confirm.questions, [])
        self.assertFalse(self.run_worktree.exists())

    def test_a_run_branch_checked_out_in_the_persons_checkout_stops_naming_it(self):
        git(self.repository.path, "stash", "-q", "--include-untracked")
        git(self.repository.path, "switch", "-q", RUN_BRANCH)
        confirm = ScriptedConfirm(CREATE)

        result = self.start_with("--worktree", PARENT, confirm=confirm)

        self.assertEqual(result.exit_code, 1)
        self.assertIn(
            f"{RUN_BRANCH} is checked out in {self.repository.path.resolve()}: switch it to "
            "another branch there, or run without --worktree",
            result.stderr,
        )
        self.assertEqual(confirm.questions, [])
        self.assertEqual(result.persistent_terminal.restarts, [])
        self.assertFalse(self.run_worktree.exists())
        self.assertEqual(self.current_branch(), RUN_BRANCH)
        self.assertEqual(git(self.repository.path, "rev-parse", "HEAD"), self.local_head)

    def test_a_path_that_is_not_the_run_worktree_stops(self):
        self.run_worktree.mkdir(parents=True)
        (self.run_worktree / "notes.txt").write_text("mine\n", encoding="utf-8")
        confirm = ScriptedConfirm(CREATE)

        result = self.start_with("--worktree", PARENT, confirm=confirm)

        self.assert_stopped(
            result,
            f"{self.run_worktree} already exists but is not the run worktree of {RUN_BRANCH}: "
            "move it away, or run without --worktree",
        )
        self.assertEqual(confirm.questions, [])
        self.assertEqual(os.listdir(self.run_worktree), ["notes.txt"])
        self.assertEqual(git(self.repository.path, "rev-parse", RUN_BRANCH), self.local_head)

    def test_a_deleted_worktree_on_another_branch_at_the_path_stops_before_the_question(self):
        git(self.repository.path, "worktree", "add", "-q", "-b", "other", str(self.run_worktree), "main")
        shutil.rmtree(self.run_worktree)
        confirm = ScriptedConfirm(CREATE)

        result = self.start_with("--worktree", PARENT, confirm=confirm)

        self.assert_stopped(
            result,
            f"{self.run_worktree} already exists but is not the run worktree of {RUN_BRANCH}",
        )
        self.assertEqual(confirm.questions, [])
        self.assertFalse(self.run_worktree.exists())

    def test_a_run_worktree_path_on_another_branch_stops(self):
        git(self.repository.path, "worktree", "add", "-q", "-b", "other", str(self.run_worktree), "main")
        confirm = ScriptedConfirm(CREATE)

        result = self.start_with("--worktree", PARENT, confirm=confirm)

        self.assert_stopped(
            result,
            f"{self.run_worktree} already exists but is not the run worktree of {RUN_BRANCH}",
        )
        self.assertEqual(confirm.questions, [])
        self.assertEqual(git(self.run_worktree, "branch", "--show-current"), "other")


class RunWorktreeSetupTests(RunWorktreeTestCase):
    """`worktree_setup` prepares the run worktree on every `--worktree` run, in the run
    worktree and never in the person's checkout, before the restart or any session."""

    PREPARE = "pwd -P >> prepared.txt"

    def configure_setup(self, setup):
        """Commit and push `setup` as `worktree_setup`, with its marker file ignored."""
        configuration = self.repository.path / "macatia.toml"
        configuration.write_text(
            f"worktree_setup = {json.dumps(setup)}\n" + configuration.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        with (self.repository.path / ".gitignore").open("a", encoding="utf-8") as ignored:
            ignored.write("prepared.txt\n")
        git(self.repository.path, "commit", "-q", "-am", "Prepare run worktrees")
        git(self.repository.path, "push", "-q", "origin", "main")
        self.base_head = self.repository.head()

    def prepared_in(self):
        """The directories `worktree_setup` ran in, once per line."""
        marker = self.run_worktree / "prepared.txt"
        if not marker.exists():
            return []
        return [pathlib.Path(line) for line in marker.read_text(encoding="utf-8").splitlines()]

    def add_run_worktree(self):
        git(self.repository.path, "worktree", "add", "-q", "-b", RUN_BRANCH, str(self.run_worktree), "main")

    def capturing_the_terminal(self, start, *arguments, **keywords):
        """Call `start`, returning its result and what was written to the process's standard
        output, where a command not captured by Macatia writes."""
        with tempfile.TemporaryFile() as terminal:
            sys.stdout.flush()
            saved = os.dup(1)
            os.dup2(terminal.fileno(), 1)
            try:
                result = start(*arguments, **keywords)
            finally:
                os.dup2(saved, 1)
                os.close(saved)
            terminal.seek(0)
            return result, terminal.read().decode()

    def test_create_prepares_the_run_worktree_then_restarts_in_it(self):
        self.configure_setup(f'{self.PREPARE} && echo "dependencies installed"')
        self.persons_checkout()

        result, terminal = self.capturing_the_terminal(
            self.start_with, "--worktree", PARENT, confirm=ScriptedConfirm(CREATE)
        )

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(self.prepared_in(), [self.run_worktree])
        self.assertIn("dependencies installed", terminal)
        self.assertLess(
            result.stdout.index(f"Preparing the run worktree {self.run_worktree}"),
            result.stdout.index("Restarting the run"),
        )
        self.assertEqual(len(result.persistent_terminal.restarts), 1)
        self.assertEqual(git(self.run_worktree, "status", "--porcelain"), "")
        self.assertFalse((self.repository.path / "prepared.txt").exists())
        self.assert_checkout_untouched()
        self.assertEqual(self.sessions.requests, [])

    def test_continue_prepares_the_existing_run_worktree_then_restarts_in_it(self):
        self.configure_setup(self.PREPARE)
        self.add_run_worktree()
        self.persons_checkout()

        result = self.start_with("--worktree", PARENT, confirm=ScriptedConfirm(RESUME))

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(self.prepared_in(), [self.run_worktree])
        self.assertEqual(len(result.persistent_terminal.restarts), 1)
        self.assertFalse((self.repository.path / "prepared.txt").exists())
        self.assert_checkout_untouched()

    def test_the_restarted_run_prepares_its_run_worktree_again_before_its_sessions(self):
        self.configure_setup(self.PREPARE)
        self.persons_checkout()
        restarted = self.start_with("--worktree", PARENT, confirm=ScriptedConfirm(CREATE))
        [restart] = restarted.persistent_terminal.restarts

        result = self.start_inside(
            restart.directory, *shlex.split(restart.command)[2:], confirm=ScriptedConfirm(CREATE),
            results=[ScriptedResult("committed")],
        )

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.prepared_in(), [self.run_worktree, self.run_worktree])
        self.assertFalse((self.repository.path / "prepared.txt").exists())
        self.assert_checkout_untouched()

    def test_with_tmux_the_run_worktree_is_prepared_once_before_the_restart(self):
        self.configure_setup(self.PREPARE)
        self.persons_checkout()
        self.sessions = ScriptedSessions(self.repository)

        result = start_run(
            self.repository, self.github, self.sessions, "--worktree", "--tmux", PARENT,
            confirm=ScriptedConfirm(CREATE),
            choose_continuation=ScriptedContinuation(Continuation.RESTART), terminal=True,
        )

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(self.prepared_in(), [self.run_worktree])
        self.assertEqual(
            result.persistent_terminal.restarts,
            [Restart(PERSISTENT_TERMINAL, f"macatia run --worktree {PARENT}",
                     self.run_worktree, sleep_lock_held=False)],
        )
        self.assert_checkout_untouched()

    def test_a_failing_setup_stops_the_run_before_the_restart(self):
        self.configure_setup("echo cannot install; exit 3")
        self.persons_checkout()

        result, terminal = self.capturing_the_terminal(
            self.start_with, "--worktree", PARENT, confirm=ScriptedConfirm(CREATE)
        )

        self.assertEqual(result.exit_code, 1)
        self.assertIn(
            f"worktree_setup exited with 3 in {self.run_worktree}: fix it, or run without "
            "--worktree",
            result.stderr,
        )
        self.assertIn("cannot install", terminal)
        self.assertEqual(result.persistent_terminal.restarts, [])
        self.assertEqual(self.sessions.requests, [])
        self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})
        self.assert_checkout_untouched()

    def test_a_failing_setup_stops_a_run_in_its_run_worktree_before_any_session(self):
        self.configure_setup("exit 4")
        self.add_run_worktree()

        result = self.start_inside(
            self.run_worktree, "--worktree", PARENT, confirm=ScriptedConfirm(CREATE),
            results=[ScriptedResult("committed")],
        )

        self.assertEqual(result.exit_code, 1)
        self.assertIn(
            f"worktree_setup exited with 4 in {self.run_worktree}: fix it, or run without "
            "--worktree",
            result.stderr,
        )
        self.assertEqual(result.persistent_terminal.restarts, [])
        self.assertEqual(self.sessions.requests, [])
        self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})

    def test_a_setup_that_dirties_the_run_worktree_stops_the_run_before_any_session(self):
        self.configure_setup("touch generated.txt")
        self.add_run_worktree()

        result = self.start_inside(
            self.run_worktree, "--worktree", PARENT, confirm=ScriptedConfirm(CREATE),
            results=[ScriptedResult("committed")],
        )

        self.assertEqual(result.exit_code, 1)
        self.assertIn("worktree is dirty at startup", result.stderr)
        self.assertEqual(self.sessions.requests, [])
        self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})

    def test_without_worktree_setup_nothing_runs(self):
        self.persons_checkout()

        result = self.start_with("--worktree", PARENT, confirm=ScriptedConfirm(CREATE))

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertNotIn("Preparing the run worktree", result.stdout)
        self.assertEqual(self.prepared_in(), [])

    def test_a_run_without_worktree_never_runs_it(self):
        self.configure_setup(self.PREPARE)
        git(self.repository.path, "switch", "-q", "--create", RUN_BRANCH)

        result = self.start_with(PARENT, results=[ScriptedResult("committed")])

        self.assert_completed(result, sessions=1)
        self.assertNotIn("Preparing the run worktree", result.stdout)
        self.assertFalse((self.repository.path / "prepared.txt").exists())


if __name__ == "__main__":
    unittest.main()
