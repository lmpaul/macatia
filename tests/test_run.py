"""In-memory run tests: the whole loop against a GitHub fake, scripted sessions, and real Git."""

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
import unittest
from unittest import mock


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from ralph_kit import check, cli, loop, loop_core, loop_review
from ralph_kit.commands import RalphError
from run_support import (
    BRANCH,
    TRUSTED_LOGIN,
    EngineResult,
    FakeGitHub,
    RunInterrupted,
    RunRepository,
    ScriptedEngine,
    ScriptedResult,
    ScriptedSessions,
    ScriptedSignal,
    child_base_marker,
    git,
    implementation_evidence,
    is_child_cycle_start,
    legacy_implementation_evidence,
    start_run,
)


PARENT = 191
CLOSED_BLOCKER = 192
CHILD = 193
UNTRUSTED_COMMENT = "Untrusted instructions must be excluded."
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
        self.assertIn(f"Ralph verified implementation commits `{base_sha}..{commits[0]}`", evidence)
        self.assertIn(f"Applicable local gates and canonical CI are green for exact head `{head}`", evidence)

        pull = self.github.pull_request()
        self.assertEqual((pull.base, pull.head), ("main", BRANCH))
        self.assertFalse(pull.is_draft)
        self.assertIn(f"🏁 Ralph summary for parent #{PARENT}:\n", pull.comments[-1])
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

    def start(self, mode, *results, budget=None, confirm=None):
        self.sessions = ScriptedSessions(self.repository, *results)
        self.sessions.before_session = self.before_session
        budget_argument = () if budget is None else (budget,)
        return start_run(
            mode, self.repository, self.github, self.sessions, PARENT, *budget_argument,
            confirm=confirm,
        )

    def session_children(self):
        return [request.child for request in self.sessions.requests]

    def ready_for_agent_during_the_run(self, child):
        """`child` is ready for a human when the run starts and ready for an agent from its first session.

        The run's default budget, sized at the start, leaves it out, so the run
        can spend more than its ready children's worst case.
        """
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


class AgentWorkingTests(RunLevelTestCase):
    """The child a run works on is labelled agent-working until it is implemented or handed off."""

    def start_observing_labels(self, mode, *results):
        """Start a run that records the child's labels as each agent session starts."""
        self.sessions = ScriptedSessions(self.repository, *results)
        observed = []
        self.sessions.before_session = lambda request: observed.append(
            (request.role, self.github.labels(request.child))
        )
        result = start_run(mode, self.repository, self.github, self.sessions, PARENT)
        return result, observed

    def test_child_is_agent_working_during_every_session_of_its_child_cycle(self):
        self.github.add_issue(CHILD, parent=PARENT)
        findings = ScriptedResult("needs_correction", findings=("Handle empty carts.",))

        result, observed = self.start_observing_labels(
            loop_review.MODE,
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

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.github.labels(CHILD), {"implemented", "documentation"})

    def test_child_handed_to_human_review_carries_only_the_human_review_lifecycle_label(self):
        self.github.add_issue(CHILD, parent=PARENT)

        def human_adds_lifecycle_label(number, body):
            if number == CHILD and not is_child_cycle_start(body):
                self.github.edit_issue_labels(CHILD, add="ready-for-agent", remove=())

        self.github.after_comment = human_adds_lifecycle_label

        result, observed = self.start_observing_labels(
            loop.MODE, ScriptedResult("blocked", "Needs a product decision.")
        )

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(observed, [("implementation", {"agent-working"})])
        self.assertEqual(self.github.labels(CHILD), {"human-review"})

    def test_failed_agent_working_label_write_stops_before_any_session(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.label_edit_error = RalphError("gh issue edit failed: HTTP 502")

        result = self.start(loop.MODE)

        self.assert_stopped_before_any_session(result, "gh issue edit failed: HTTP 502")
        self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})


class ScriptedConfirm:
    """Answers the run's startup question as the person at the terminal would.

    `answer` is True (Resume), False (Stop), or an exception the prompt raises.
    """

    def __init__(self, answer):
        self.answer = answer
        self.questions = []

    def __call__(self, question):
        self.questions.append(question)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


STOP, RESUME = False, True


class LeftAgentWorkingTests(RunLevelTestCase):
    """A run that starts with children left agent working asks before resuming them."""

    def assert_warning(self, text, *children):
        names = ", ".join(f"#{child}" for child in children)
        self.assertIn(f"{names} labelled agent-working", text)
        self.assertIn("Ralph cannot tell an interrupted run from a run still active elsewhere", text)
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
                f"#{child} ready-for-agent, then run `ralph-kit run {PARENT}` again.",
                summary,
            )
        self.assertNotIn("Fix the stop reason", summary)
        return summary

    def test_stop_leaves_the_child_agent_working_and_starts_no_session(self):
        self.github.add_issue(CHILD, parent=PARENT, labels=("agent-working",))
        self.github.add_issue(DEPENDENT, parent=PARENT)
        confirm = ScriptedConfirm(STOP)

        result = self.start(loop.MODE, confirm=confirm)

        self.assert_stopped_on_agent_working(result, CHILD)
        self.assertEqual(self.github.labels(DEPENDENT), {"ready-for-agent"})
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

        result = self.start(loop.MODE, *passing_child_results(loop.MODE), confirm=confirm)

        self.assert_completed(result, sessions=1)
        self.assertEqual(len(confirm.questions), 1)
        self.assertEqual(self.session_children(), [CHILD])
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_resumed_child_counts_in_the_default_run_budget(self):
        self.github.add_issue(CHILD, parent=PARENT, labels=("agent-working",))
        self.ready_for_agent_during_the_run(READY_FOR_HUMAN_CHILD)

        result = self.start(
            loop_review.MODE, *worst_case_child_results(loop_review.MODE),
            ScriptedResult("committed"), confirm=ScriptedConfirm(RESUME),
        )

        self.assertEqual(result.exit_code, 1)
        self.assertIn("run budget exhausted before review", result.stderr)
        self.assertEqual(len(self.sessions.requests), 9)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_several_children_get_one_warning_and_one_choice(self):
        self.github.add_issue(CHILD, parent=PARENT, labels=("agent-working",))
        self.github.add_issue(DEPENDENT, parent=PARENT, labels=("agent-working",))
        confirm = ScriptedConfirm(RESUME)

        result = self.start(
            loop.MODE, *passing_child_results(loop.MODE) * 2, confirm=confirm
        )

        self.assert_completed(result, sessions=2)
        (question,) = confirm.questions
        self.assertEqual((result.stdout + question).count("labelled agent-working"), 1)
        self.assert_warning(result.stdout + question, CHILD, DEPENDENT)
        self.assertEqual(self.session_children(), [CHILD, DEPENDENT])

    def test_several_children_stopped_are_each_listed(self):
        self.github.add_issue(CHILD, parent=PARENT, labels=("agent-working",))
        self.github.add_issue(DEPENDENT, parent=PARENT, labels=("agent-working",))

        result = self.start(loop.MODE, confirm=ScriptedConfirm(STOP))

        self.assert_stopped_on_agent_working(result, CHILD, DEPENDENT)

    def test_without_a_terminal_the_run_stops_with_the_warning_in_its_summary(self):
        self.github.add_issue(CHILD, parent=PARENT, labels=("agent-working",))

        result = self.start(loop.MODE, confirm=None)

        summary = self.assert_stopped_on_agent_working(result, CHILD)
        self.assert_warning(summary, CHILD)

    def test_ctrl_c_at_the_prompt_counts_as_stop(self):
        self.github.add_issue(CHILD, parent=PARENT, labels=("agent-working",))

        result = self.start(loop.MODE, confirm=ScriptedConfirm(KeyboardInterrupt()))

        self.assert_stopped_on_agent_working(result, CHILD)

    def test_published_agent_working_child_is_recovered_without_a_prompt(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.interrupt_after_comment_on = CHILD
        self.github.interrupt_after_comment_containing = loop.MODE.implementation_evidence_prefix
        with self.assertRaises(RunInterrupted):
            self.start(loop.MODE, *passing_child_results(loop.MODE))
        self.assertEqual(self.github.labels(CHILD), {"agent-working"})
        confirm = ScriptedConfirm(STOP)

        result = self.start(loop.MODE, confirm=confirm)

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
        return start_run(mode, self.repository, self.github, self.sessions, PARENT)

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
                f"not be running, then run `ralph-kit run {PARENT}` again if this one should "
                "continue.",
                published,
            )
            self.assertNotIn("Fix the stop reason", published)

    def test_run_stops_at_its_next_child_selection_without_another_session(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.add_issue(DEPENDENT, parent=PARENT)
        self.github.add_issue(LATER_CHILD, parent=PARENT)

        result = self.start_with_another_run(loop.MODE, *passing_child_results(loop.MODE))

        self.assert_stopped_on_another_run(result)
        self.assertEqual(self.github.labels(LATER_CHILD), {"ready-for-agent"})

    def test_published_child_of_another_run_is_left_to_that_run(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.add_issue(DEPENDENT, parent=PARENT)

        result = self.start_with_another_run(
            loop.MODE, *passing_child_results(loop.MODE), published=True
        )

        self.assert_stopped_on_another_run(result, dependent_comments=1)

    def test_own_agent_working_child_does_not_stop_the_run(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.add_issue(DEPENDENT, parent=PARENT)

        result = self.start(loop_review.MODE, *passing_child_results(loop_review.MODE) * 2)

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
        self.assertIn(f"Ralph verified implementation commits `{child_base}..", evidence)

    def test_child_cycle_start_records_the_child_base_after_marking_the_child_agent_working(self):
        self.github.add_issue(CHILD, parent=PARENT)
        labels_at_start = []

        def observe_labels_at_start(number, body):
            if is_child_cycle_start(body):
                labels_at_start.append(self.github.labels(number))

        self.github.after_comment = observe_labels_at_start

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

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
        self.hand_off(loop_review.MODE, "first-cycle.txt")
        human_commit = self.commit_file("human-fix.txt", "Fix during human review")
        self.relabel_ready()
        self.hand_off(loop_review.MODE, "second-cycle.txt")
        self.relabel_ready()

        result = self.start(
            loop_review.MODE, ScriptedResult("committed"), ScriptedResult("no_changes")
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
            self.start(loop.MODE, RunInterrupted("the run's process died"))
        self.assertEqual(self.github.labels(CHILD), {"agent-working"})
        self.commit_file("partial.txt", "Partial work of the interrupted child cycle")
        self.forget_diagnostics()

        result = self.start(
            loop_review.MODE, ScriptedResult("committed"), ScriptedResult("no_changes"),
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

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

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

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

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

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

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

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

        self.assert_completed(result, sessions=1)
        self.assertNotIn("no longer on the branch", self.start_comment(CHILD))
        self.assert_evidence_starts_from(head)

    def test_started_child_stopped_by_a_dependency_deadlock_keeps_its_commits(self):
        self.commit_prior_child()
        self.github.add_issue(CHILD, parent=PARENT, blocked_by=[OTHER_CHILD], comments=[
            child_base_marker(self.repository.base_sha),
        ])
        self.github.add_issue(OTHER_CHILD, parent=PARENT, blocked_by=[CHILD])

        result = self.start(loop.MODE)

        self.assert_stopped_before_any_session(result, "dependency deadlock")
        self.assertIn(COMMITS_STAY, self.github.comments(CHILD)[-1])
        self.assertNotIn(COMMITS_STAY, self.github.comments(OTHER_CHILD)[-1])

    def test_child_whose_child_base_left_the_branch_is_stopped_by_a_deadlock_without_commits(self):
        self.github.add_issue(CHILD, parent=PARENT, blocked_by=[OTHER_CHILD], comments=[
            child_base_marker(self.commit_off_the_branch()),
        ])
        self.github.add_issue(OTHER_CHILD, parent=PARENT, blocked_by=[CHILD])

        result = self.start(loop.MODE)

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
            comments=[*comments, implementation_evidence(loop.MODE, verified_head)],
        )

    def assert_names_uncovered_commits(self, uncovered_range):
        uncovered = f"`{uncovered_range}`"
        self.assertIn(uncovered, self.start_comment(CHILD))
        self.assertIn(uncovered, self.github.comments(CHILD)[-1])

    def test_child_resumed_after_another_child_was_implemented_on_top_moves_its_child_base(self):
        self.github.add_issue(CHILD, parent=PARENT)
        self.github.add_issue(OTHER_CHILD, parent=PARENT)
        base_sha = self.repository.base_sha
        self.hand_off(loop_review.MODE, "a1.txt")
        self.relabel_ready()
        earlier_cycles_head = self.hand_off(loop_review.MODE, "a2.txt")
        other_child_head = self.implement_other_child(loop_review.MODE)
        human_commit = self.commit_file("human-fix.txt", "Fix during human review")
        self.relabel_ready()

        result = self.start(
            loop_review.MODE, ScriptedResult("committed"), ScriptedResult("no_changes")
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
        earlier_cycles_head = self.hand_off(loop.MODE, "a1.txt")
        other_child_head = self.commit_file("other.txt", "Implement the other child")
        later_child_head = self.commit_file("later.txt", "Implement the later child")
        self.add_implemented_child(
            LATER_CHILD, later_child_head, child_base_marker(other_child_head)
        )
        self.add_implemented_child(
            OTHER_CHILD, other_child_head, child_base_marker(earlier_cycles_head)
        )
        self.relabel_ready()

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

        self.assert_completed(result, sessions=1)
        self.assert_evidence_starts_from(later_child_head)
        self.assert_names_uncovered_commits(f"{base_sha}..{earlier_cycles_head}")
        self.assertEqual(self.local_gate_bases(), [base_sha])

    def test_child_cycles_between_other_children_are_named_as_one_inexact_range(self):
        self.github.add_issue(CHILD, parent=PARENT)
        base_sha = self.repository.base_sha
        first_cycle_head = self.hand_off(loop.MODE, "a1.txt")
        other_child_head = self.commit_file("other.txt", "Implement the other child")
        self.add_implemented_child(
            OTHER_CHILD, other_child_head, child_base_marker(first_cycle_head)
        )
        self.relabel_ready()
        second_cycle_head = self.hand_off(loop.MODE, "a2.txt")
        later_child_head = self.commit_file("later.txt", "Implement the later child")
        self.add_implemented_child(
            LATER_CHILD, later_child_head, child_base_marker(second_cycle_head)
        )
        self.relabel_ready()

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

        self.assert_completed(result, sessions=1)
        self.assert_evidence_starts_from(later_child_head)
        self.assert_names_uncovered_commits(f"{base_sha}..{second_cycle_head}")
        self.assertIn("and other children's work", self.start_comment(CHILD))
        self.assertEqual(self.local_gate_bases(), [base_sha])

    def test_child_cycle_after_another_child_is_named_when_the_first_left_no_commits(self):
        self.github.add_issue(CHILD, parent=PARENT)
        base_sha = self.repository.base_sha
        result = self.start(loop.MODE, ScriptedResult("blocked", "Needs a decision."))
        self.assertEqual(self.github.labels(CHILD), {"human-review"}, result.stderr)
        self.forget_diagnostics()
        other_child_head = self.commit_file("other.txt", "Implement the other child")
        self.add_implemented_child(OTHER_CHILD, other_child_head, child_base_marker(base_sha))
        self.relabel_ready()
        second_cycle_head = self.hand_off(loop.MODE, "a2.txt")
        later_child_head = self.commit_file("later.txt", "Implement the later child")
        self.add_implemented_child(
            LATER_CHILD, later_child_head, child_base_marker(second_cycle_head)
        )
        self.relabel_ready()

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

        self.assert_completed(result, sessions=1)
        self.assert_evidence_starts_from(later_child_head)
        self.assert_names_uncovered_commits(f"{other_child_head}..{second_cycle_head}")
        self.assertIn("from this child's earlier child cycles", self.start_comment(CHILD))
        self.assertEqual(self.local_gate_bases(), [base_sha])

    def test_other_child_without_a_recorded_child_base_names_every_commit_before_the_child_base(self):
        self.github.add_issue(CHILD, parent=PARENT)
        base_sha = self.repository.base_sha
        self.hand_off(loop.MODE, "a1.txt")
        other_child_head = self.commit_file("other.txt", "Implement the other child")
        self.add_implemented_child(OTHER_CHILD, other_child_head)
        self.relabel_ready()

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

        self.assert_completed(result, sessions=1)
        self.assert_evidence_starts_from(other_child_head)
        self.assert_names_uncovered_commits(f"{base_sha}..{other_child_head}")

    def test_child_implemented_before_the_recorded_child_base_does_not_move_it(self):
        prior = self.commit_prior_child()
        self.add_implemented_child(OTHER_CHILD, prior)
        self.github.add_issue(CHILD, parent=PARENT)
        self.hand_off(loop.MODE, "a1.txt")
        self.relabel_ready()

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

        self.assert_completed(result, sessions=1)
        self.assert_evidence_starts_from(prior)
        self.assertNotIn("not covered", self.start_comment(CHILD))
        self.assertNotIn("not covered", self.github.comments(CHILD)[-1])
        self.assertEqual(self.local_gate_bases(), [prior])


class StartupChoiceCommandLineTests(unittest.TestCase):
    """`ralph-kit run` asks the startup question with the arrow keys only in a terminal."""

    def confirm_passed(self, *, stdin_tty, stdout_tty, review=False):
        with (
            mock.patch.object(sys.stdin, "isatty", return_value=stdin_tty),
            mock.patch.object(sys.stdout, "isatty", return_value=stdout_tty),
            mock.patch.object(loop, "run", return_value=0) as standard,
            mock.patch.object(loop_review, "run", return_value=0) as reviewed,
            mock.patch.object(sys, "dont_write_bytecode", sys.dont_write_bytecode),
        ):
            self.assertEqual(cli.main(["run", *(["--review"] if review else []), "5"]), 0)
        return (reviewed if review else standard).call_args.kwargs["confirm"]

    def test_both_terminals_get_the_arrow_key_selector(self):
        for review in (False, True):
            with self.subTest(review=review):
                confirm = self.confirm_passed(stdin_tty=True, stdout_tty=True, review=review)
                self.assertIs(confirm, cli.resume_with_arrows)

    def test_without_both_terminals_there_is_no_selector(self):
        for stdin_tty, stdout_tty in ((False, True), (True, False), (False, False)):
            with self.subTest(stdin_tty=stdin_tty, stdout_tty=stdout_tty):
                self.assertIsNone(self.confirm_passed(stdin_tty=stdin_tty, stdout_tty=stdout_tty))

    def test_selector_offers_stop_by_default_and_resumes_only_on_resume(self):
        for choice, resume in (("Stop", False), ("Resume", True)):
            with (
                self.subTest(choice=choice),
                mock.patch.object(check, "arrow_select", return_value=choice) as select,
            ):
                self.assertIs(cli.resume_with_arrows("Resume #193?"), resume)
                select.assert_called_once_with(
                    "Resume #193?", [("Stop", ""), ("Resume", "")], "Stop"
                )


class ParentCompletionTests(RunLevelTestCase):
    def test_all_children_complete_hands_the_parent_to_human_review_without_sessions(self):
        self.new_run(parent_labels=("ready-for-agent", "implemented", "documentation"))
        self.github.add_issue(CLOSED_BLOCKER, parent=PARENT, state="closed", labels=())
        self.github.add_issue(CHILD, parent=PARENT, labels=("implemented",))

        result = self.start(loop.MODE)

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertIn("- agent sessions: 0\n- infrastructure retries: 0\n", result.stdout)
        self.assertIn("implemented children this run: 0", result.stdout)
        self.assertEqual(self.github.labels(PARENT), {"human-review", "documentation"})
        parent_comments = self.github.comments(PARENT)
        self.assertIn("<!-- ralph-experimental-parent-completion -->", parent_comments[-2])
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


class RepeatedRunTests(RunLevelTestCase):
    def test_second_run_in_the_same_process_does_not_share_the_first_runs_state(self):
        self.github.add_issue(CHILD, parent=PARENT)
        first = self.start(loop.MODE, ScriptedResult("committed"))
        self.assert_completed(first, sessions=1)

        second = self.start(loop.MODE)

        self.assert_completed(second, sessions=0)
        self.assertEqual(self.sessions.requests, [])
        first_summary, second_summary = (
            comment for comment in self.github.comments(PARENT)
            if loop.MODE.terminal_summary_marker in comment
        )
        self.assertIn("- implemented children this run: 1\n", first_summary)
        self.assertIn("- agent sessions: 1\n", first_summary)
        self.assertIn("- implemented children this run: 0\n", second_summary)
        self.assertIn("- agent sessions: 0\n", second_summary)
        runs = list((self.repository.path / ".ralph" / "diagnostics").glob("*/run.json"))
        self.assertEqual(len(runs), 2)


class RunDiagnosticsTests(RunLevelTestCase):
    """The repository's own ignore rules say nothing about `.ralph/`."""

    def setUp(self):
        super().setUp()
        self.github.add_issue(CHILD, parent=PARENT)
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", ".ralph/"], cwd=self.repository.path, check=False
        )
        self.assertEqual(ignored.returncode, 1, "the fixture must not ignore .ralph/")

    def test_run_diagnostics_never_make_the_worktree_dirty(self):
        # Real session handling records each session in `.ralph/` and checks the worktree after it.
        engine = ScriptedEngine(self.repository, EngineResult(), EngineResult())
        result = start_run(loop_review.MODE, self.repository, self.github, engine, PARENT)

        self.assert_completed(result, sessions=2)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        self.assertEqual(len(self.repository.session_directories()), 2)
        self.assertTrue(self.repository.is_clean())

    def test_maintainer_files_in_the_diagnostics_directory_are_kept(self):
        own = {".gitignore": "# Kept by the maintainer\n*\n", "notes.txt": "Mine.\n"}
        for name, text in own.items():
            path = self.repository.path / ".ralph" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(text, encoding="utf-8")

        result = self.start(loop.MODE, ScriptedResult("committed"))

        self.assert_completed(result, sessions=1)
        for name, text in own.items():
            self.assertEqual((self.repository.path / ".ralph" / name).read_text(encoding="utf-8"), text)
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
    def test_base_branch_is_rejected_before_any_session(self):
        git(self.repository.path, "checkout", "main")

        result = self.start(loop.MODE)

        self.assert_rejected_at_startup(result, "main is not a safe Ralph branch")

    def test_detached_head_is_rejected_before_any_session(self):
        git(self.repository.path, "checkout", "--detach", self.repository.base_sha)

        result = self.start(loop.MODE)

        self.assert_rejected_at_startup(result, "detached HEAD is not allowed")

    def test_dirty_worktree_is_rejected_before_any_session(self):
        (self.repository.path / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")

        result = self.start(loop.MODE)

        self.assert_rejected_at_startup(result, "worktree is dirty at startup")

    def test_checkout_that_is_not_the_selected_worktree_is_rejected_before_any_session(self):
        for name, directory in (
            ("outside the repository", self.repository.path.parent),
            ("inside the repository", self.repository.path / ".github"),
        ):
            with self.subTest(name):
                self.sessions = ScriptedSessions(self.repository)

                result = start_run(
                    loop.MODE, self.repository, self.github, self.sessions, PARENT, cwd=directory
                )

                self.assert_rejected_at_startup(result, "wrong worktree")

    def test_wrong_repository_is_rejected_before_any_session(self):
        self.github.repository = "someone/else"

        result = self.start(loop.MODE)

        self.assert_rejected_at_startup(
            result, "wrong repository: expected lmpaul/buyr, found someone/else"
        )

    def test_child_not_ready_for_an_agent_is_rejected_before_any_session(self):
        self.github.edit_issue_labels(CHILD, "needs-info", ())

        result = self.start(loop.MODE)

        self.assert_stopped_before_any_session(
            result, f"parent issue #{PARENT} has children remaining but none is runnable"
        )
        self.assertEqual(self.github.labels(CHILD), {"ready-for-agent", "needs-info"})

    def test_arguments_must_be_positive_integers(self):
        for arguments in (("0",), ("abc",), (PARENT, "0"), (PARENT, "abc")):
            with self.subTest(arguments=arguments):
                self.sessions = ScriptedSessions(self.repository)

                result = start_run(loop.MODE, self.repository, self.github, self.sessions, *arguments)

                self.assert_rejected_at_startup(result, "must be a positive integer")


class ConfigurationRejectionTests(StartupRejectionTestCase):
    def test_verification_flags_that_are_not_toml_booleans_are_rejected_before_any_session(self):
        for key in ("local_checks", "ci_checks"):
            for value in ('"true"', '"false"', "1", "0"):
                with self.subTest(key=key, value=value):
                    self.repository = RunRepository(self)
                    self.repository.edit_configuration(f"{key} = true", f"{key} = {value}")

                    result = self.start(loop.MODE)

                    self.assert_rejected_at_startup(
                        result, f"configuration.verification.{key} must be a boolean"
                    )

    def test_legacy_verification_keys_are_rejected_before_any_session(self):
        self.repository.edit_configuration(
            "local_checks = true\nci_checks = true", 'mode = "ci"\nlocal_gates = "enabled"'
        )

        result = self.start(loop.MODE)

        self.assert_rejected_at_startup(result, "unknown keys: local_gates, mode")

    def test_duplicate_toml_key_is_rejected_before_any_session(self):
        self.repository.edit_configuration("ci_checks = true\n", "ci_checks = true\nci_checks = true\n")

        result = self.start(loop.MODE)

        self.assert_rejected_at_startup(result, "invalid TOML")

    def test_missing_agent_working_label_is_rejected_before_any_session(self):
        self.repository.edit_configuration('agent_working = "agent-working"\n', "")

        result = self.start(loop.MODE)

        self.assert_rejected_at_startup(
            result, "configuration.labels is missing keys: agent_working"
        )

    def test_duplicate_label_value_is_rejected_before_any_session(self):
        self.repository.edit_configuration(
            'agent_working = "agent-working"', 'agent_working = "implemented"'
        )

        result = self.start(loop.MODE)

        self.assert_rejected_at_startup(result, "configuration.labels values must be unique")

    def test_unknown_engine_is_rejected_before_any_session(self):
        self.repository = RunRepository(self, engines={"implementation": "unknown"})

        result = self.start(loop.MODE)

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

        result = self.start(loop.MODE)

        self.assert_rejected_at_startup(
            result, "configuration.models.implementation.reasoning_effort must be one of"
        )

    def remove_review_profile(self):
        configuration = (self.repository.path / "ralph.toml").read_text(encoding="utf-8")
        review_profile = re.search(r"\[models\.review\]\n.*?\n\n", configuration, flags=re.S).group(0)
        self.repository.edit_configuration(review_profile, "")

    def test_review_profile_is_optional_for_a_standard_run(self):
        self.remove_review_profile()

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

        self.assert_completed(result, sessions=1)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_review_profile_is_required_for_a_reviewed_run(self):
        self.remove_review_profile()

        result = self.start(loop_review.MODE, *passing_child_results(loop_review.MODE))

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

                result = self.start(loop.MODE, *passing_child_results(loop.MODE))

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

                result = self.start(loop.MODE)

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

                result = self.start(loop.MODE)

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

        result = self.start(loop.MODE)

        self.assert_rejected_at_startup(
            result,
            "configuration.models.backup must differ from configuration.models.correction "
            "in engine, model, or reasoning effort",
        )


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

        result = self.start(loop.MODE, *passing_child_results(loop.MODE))

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

        result = self.start(loop.MODE)

        self.assert_stopped_before_any_session(
            result, f"issue #{CHILD} must be authored by one of: @{TRUSTED_LOGIN}"
        )

    def test_untrusted_implementation_evidence_never_satisfies_a_blocker(self):
        verified_head = self.commit_prior_child()
        evidence = implementation_evidence(loop.MODE, verified_head)
        self.github.add_issue(
            BLOCKER,
            parent=PARENT,
            labels=("implemented",),
            comments=[evidence | {"author": {"login": "outside-contributor"}}],
        )
        self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[BLOCKER])

        result = self.start(loop.MODE)

        self.assert_stopped_before_any_session(
            result, f"parent issue #{PARENT} has children remaining but none is runnable"
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

    def test_empty_job_log_is_never_used_as_evidence(self):
        self.new_child_run(ci=("failure", "success"), job_log_output="")

        result = self.start(loop.MODE, ScriptedResult("committed"), ScriptedResult("committed"))

        self.assert_implemented(result)
        self.assertIn("CI logs unavailable. The downloaded job log is empty.", self.sessions.prompts[1])
        (evidence,) = self.repository.ci_evidence()
        self.assertFalse(evidence["logs_available"])
        self.assertEqual(evidence["log_error"], "The downloaded job log is empty.")

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
            if number == CHILD and not is_child_cycle_start(body):
                self.github.after_comment = None
                self.commit_file("advance.txt", "Advance after CI")

        self.github.after_comment = advance_the_branch

        result = self.start(loop.MODE, ScriptedResult("committed"))

        self.assert_not_implemented(result, "branch advanced after canonical CI")

    def test_branch_advancing_while_ci_is_pending_never_authorizes_implemented(self):
        self.new_child_run(ci=({CANONICAL_CHECK: {"status": "in_progress", "conclusion": None}}, "success"))

        def advance_the_branch(head_sha):
            self.github.before_check_runs = None
            self.commit_file("advance.txt", "Advance while CI is pending")

        self.github.before_check_runs = advance_the_branch

        result = self.start(loop.MODE, ScriptedResult("committed"))

        self.assert_not_implemented(result, "branch advanced while waiting for canonical CI")
        self.assertEqual(len(self.github.checked_heads), 1)

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

    def test_pending_ci_reports_progress_every_five_minutes_without_a_terminal(self):
        pending = {"status": "in_progress", "conclusion": None}
        self.new_child_run(
            ci=({CANONICAL_CHECK: "success", DATABASE_CHECK: pending},) * 15 + ("success",),
            check_names=(CANONICAL_CHECK, DATABASE_CHECK),
        )

        with mock.patch.dict(os.environ):
            os.environ.pop("RALPH_CI_POLL_SECONDS", None)
            result = self.start(loop.MODE, ScriptedResult("committed", path=PERSISTENCE_PATH))

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
            self.start(loop.MODE, crash)

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
        rerun = f"Ralph was interrupted: run `ralph-kit run {PARENT}` again to continue."
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
        self.assertTrue(handoff.startswith("Ralph stopped because the run was interrupted"))
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
                f"`ralph-kit run {PARENT}` again.",
                published,
            )

    def test_ctrl_c_before_the_first_commit_returns_the_child_to_ready_for_agent(self):
        result = self.start(loop.MODE, KeyboardInterrupt())

        self.assert_interrupted(result, 130, "SIGINT")
        self.assertEqual(self.roles(), ["implementation"])
        self.assertIsNone(self.github.pull_request())
        self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})
        self.assertEqual(self.github.reports(CHILD), [])
        self.assertIn(f"- #{CHILD}: runnable.", self.github.comments(PARENT)[-1])

    def interrupt_while_waiting_for_ci(self, signal_number):
        """Interrupt the run once the child's commit waits for CI, as Ctrl-C or `kill` would."""

        def interrupt(head):
            os.kill(os.getpid(), signal_number)

        self.github.before_check_runs = interrupt

    def test_ctrl_c_after_a_commit_hands_the_child_to_human_review(self):
        base = self.repository.head()
        self.interrupt_while_waiting_for_ci(signal.SIGINT)

        result = self.start(loop.MODE, ScriptedResult("committed"))

        self.assert_interrupted(result, 130, "SIGINT", rerun_action=False)
        self.assertEqual(self.roles(), ["implementation"])
        self.assert_handed_to_human_review_unverified(result, base, self.repository.head())

    def test_sigterm_and_sighup_during_an_agent_session_behave_like_ctrl_c(self):
        for signal_number, exit_code in ((signal.SIGTERM, 143), (signal.SIGHUP, 129)):
            with self.subTest(signal_number.name, commit=False):
                self.new_child_run()
                handler = signal.getsignal(signal_number)

                result = self.start(loop.MODE, ScriptedSignal(signal_number))

                self.assert_interrupted(result, exit_code, signal_number.name)
                self.assertEqual(self.roles(), ["implementation"])
                self.assertEqual(self.github.labels(CHILD), {"ready-for-agent"})
                self.assertEqual(self.github.reports(CHILD), [])
                self.assertIs(signal.getsignal(signal_number), handler)
            with self.subTest(signal_number.name, commit=True):
                self.new_child_run()
                base = self.repository.head()
                self.interrupt_while_waiting_for_ci(signal_number)

                result = self.start(loop.MODE, ScriptedResult("committed"))

                self.assert_interrupted(
                    result, exit_code, signal_number.name, rerun_action=False
                )
                self.assert_handed_to_human_review_unverified(result, base, self.repository.head())

    def test_a_signal_ignored_at_launch_stays_ignored(self):
        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGHUP, previous)
        self.sessions = ScriptedSessions(self.repository, ScriptedResult("committed"))
        self.sessions.before_session = lambda request: os.kill(os.getpid(), signal.SIGHUP)

        result = start_run(loop.MODE, self.repository, self.github, self.sessions, PARENT)

        self.assert_implemented(result)
        self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)

    def test_ctrl_c_after_the_evidence_is_published_marks_the_child_implemented(self):
        def interrupt_after_evidence(number, body):
            if number == CHILD and not is_child_cycle_start(body):
                raise KeyboardInterrupt

        self.github.after_comment = interrupt_after_evidence

        result = self.start(loop.MODE, ScriptedResult("committed"))

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

        result = self.start(loop.MODE, ScriptedResult("committed"))

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

        result = self.start(loop.MODE, KeyboardInterrupt())

        self.assertEqual(result.exit_code, 130)
        self.assertIn("- final outcome: interrupted", self.github.comments(PARENT)[-1])
        self.assertIn(
            "Ralph was interrupted again; exiting without finishing the cleanup.", result.stderr
        )
        self.assertNotIn("Ralph summary", result.stderr)

    def test_second_interruption_during_the_release_leaves_the_child_agent_working(self):
        def interrupt_again(number, body):
            if number == CHILD and not is_child_cycle_start(body):
                raise KeyboardInterrupt

        self.github.after_comment = interrupt_again
        self.interrupt_while_waiting_for_ci(signal.SIGINT)

        result = self.start(loop.MODE, ScriptedResult("committed"))

        self.assertEqual(result.exit_code, 130)
        self.assertEqual(self.github.labels(CHILD), {"agent-working"})
        self.assertEqual(self.github.comments(PARENT), [])
        self.assertIn(
            "Ralph was interrupted again; exiting without finishing the cleanup.", result.stderr
        )

    def test_simulated_process_death_is_neither_caught_nor_reported(self):
        with self.assertRaises(RunInterrupted):
            self.start(loop.MODE, RunInterrupted("killed with SIGKILL"))

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

        result = self.start(loop.MODE, ScriptedResult("committed"))

        self.assert_not_implemented(result, "branch advanced during an infrastructure retry")
        self.assertEqual(len(self.github.checked_heads), 1)
        self.assertEqual(self.roles(), ["implementation"])
        self.assertIn("- infrastructure retries: 0\n", self.github.comments(PARENT)[-1])


CLOSED_CHILD = 195
IMPLEMENTED_CHILD = 196
HUMAN_REVIEW_CHILD = 197
READY_FOR_HUMAN_CHILD = 198


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
        self.assertIn(COMMITS_STAY, handoff)
        summary = self.github.comments(PARENT)[-1]
        self.assertIn(f"- agent sessions: {budget}\n", summary)
        self.assertIn("- final outcome: stopped safely", summary)
        self.assertIn(f"- agent sessions: {budget}\n", result.stderr)
        self.assertEqual(self.github.labels(PARENT), {"ready-for-agent"})
        return handoff

    def test_default_budget_of_a_reviewed_run_allows_nine_sessions_per_child_across_the_run(self):
        self.new_two_child_run()
        self.ready_for_agent_during_the_run(READY_FOR_HUMAN_CHILD)
        worst_case = worst_case_child_results(loop_review.MODE)

        result = self.start(loop_review.MODE, *worst_case * 2, *worst_case[:2])

        self.assert_budget_stop(
            result, READY_FOR_HUMAN_CHILD, role="issue correction", budget=18
        )
        self.assertEqual(
            self.session_children(), [CHILD] * 8 + [DEPENDENT] * 8 + [READY_FOR_HUMAN_CHILD] * 2
        )
        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        self.assertEqual(self.github.labels(DEPENDENT), {"implemented"})
        self.assertTrue(self.github.pull_request().is_draft)

    def test_default_budget_of_a_standard_run_allows_five_sessions_per_child_across_the_run(self):
        self.ready_for_agent_during_the_run(READY_FOR_HUMAN_CHILD)
        findings = ScriptedResult("needs_correction", findings=("Handle empty carts.",))

        result = self.start(loop.MODE, *worst_case_child_results(loop.MODE), findings)

        self.assert_budget_stop(
            result, READY_FOR_HUMAN_CHILD, role="issue correction", budget=5
        )
        self.assertEqual(self.session_children(), [CHILD] * 4 + [READY_FOR_HUMAN_CHILD])
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_default_budget_of_a_resumed_parent_counts_only_children_ready_for_an_agent(self):
        # CHILD, from `setUp`, is the only child ready for an agent.
        self.github.add_issue(CLOSED_CHILD, parent=PARENT, state="closed", labels=())
        self.github.add_issue(IMPLEMENTED_CHILD, parent=PARENT, labels=("implemented",))
        self.github.add_issue(HUMAN_REVIEW_CHILD, parent=PARENT, labels=("human-review",))
        self.ready_for_agent_during_the_run(READY_FOR_HUMAN_CHILD)

        result = self.start(
            loop_review.MODE, *worst_case_child_results(loop_review.MODE),
            ScriptedResult("committed"),
        )

        self.assert_budget_stop(result, READY_FOR_HUMAN_CHILD, role="review", budget=9)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})

    def test_explicit_budget_replaces_the_default_across_children(self):
        self.new_two_child_run()
        results = [*passing_child_results(loop_review.MODE), ScriptedResult("committed")]

        result = self.start(loop_review.MODE, *results, budget=3)

        self.assert_budget_stop(result, DEPENDENT, role="review", budget=3)
        self.assertEqual(self.session_children(), [CHILD, CHILD, DEPENDENT])
        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        self.assertTrue(self.github.pull_request().is_draft)


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

        result = self.start(loop_review.MODE, *passing_child_results(loop_review.MODE))

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.github.labels(CHILD), {"implemented"})
        self.assert_stop_summary(
            result,
            f"Stop reason: parent issue #{PARENT} has children remaining but none is runnable",
            f"Remaining children:\n- #{DEPENDENT}: labelled ready-for-human.",
            f"Next action:\n- #{DEPENDENT} is labelled ready-for-human: implement and close it, "
            f"or relabel it ready-for-agent, then run `ralph-kit run --review {PARENT}` again.",
        )

    def test_child_waiting_on_an_unimplemented_blocker_names_the_blocker(self):
        self.new_run()
        self.github.add_issue(CHILD, parent=PARENT, labels=("needs-info",))
        self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[CHILD])

        result = self.start(loop.MODE)

        self.assert_stop_summary(
            result,
            f"- #{CHILD}: labelled needs-info.\n- #{DEPENDENT}: waiting on #{CHILD}, which is not implemented.",
            f"- #{DEPENDENT} waits on #{CHILD}: implement or close it, then run `ralph-kit run {PARENT}` again.",
        )

    def test_dependency_deadlock_names_the_blocked_children_and_their_blockers(self):
        self.new_run()
        self.github.add_issue(BLOCKER, parent=PARENT, blocked_by=[DEPENDENT])
        self.github.add_issue(DEPENDENT, parent=PARENT, blocked_by=[BLOCKER])

        result = self.start(loop.MODE)

        self.assert_stop_summary(
            result,
            "Stop reason: parent issue has a dependency deadlock",
            f"- #{BLOCKER}: part of a dependency deadlock, waiting on #{DEPENDENT}.",
            f"- #{DEPENDENT}: part of a dependency deadlock, waiting on #{BLOCKER}.",
            f"- #{BLOCKER} is part of a dependency deadlock with #{DEPENDENT}: remove a blocked-by "
            f"link from the cycle, relabel it ready-for-agent, then run `ralph-kit run {PARENT}` again.",
        )

    def test_child_handed_to_human_review_links_its_handoff_comment(self):
        committed = ScriptedResult("committed")
        findings = ScriptedResult("needs_correction", findings=("Handle empty carts.",))
        for name, mode, results, reason in (
            ("blocked", loop.MODE, [ScriptedResult("blocked", "Needs a product decision.")],
             "implementation stopped with blocked: Needs a product decision."),
            ("failed", loop.MODE, [ScriptedResult("failed", "The toolchain crashed.")],
             "implementation stopped with failed: The toolchain crashed."),
            ("missing commit", loop.MODE, [findings, ScriptedResult("committed", commit=False)],
             "Issue correction did not create a new commit"),
            ("correction limit", loop.MODE, [findings, *[dataclasses.replace(findings, commit=True)] * 3],
             "correction limit of 3 exhausted before issue correction"),
            ("rejected work", loop_review.MODE, [ScriptedResult("no_changes"), committed],
             "implementation no_changes requires an independent review with no corrections"),
        ):
            with self.subTest(name):
                self.new_child_run()

                result = self.start(mode, *results)

                handoff = self.github.latest_comment_url(CHILD)
                command = f"ralph-kit run{' --review' if mode.review else ''} {PARENT}"
                self.assert_stop_summary(
                    result,
                    f"Stop reason: {reason}",
                    f"- #{CHILD}: handed to human review ({handoff}).",
                    f"- #{CHILD} was handed to human review: resolve what its handoff comment "
                    f"({handoff}) reports, then close it or relabel it ready-for-agent, "
                    f"and run `{command}` again.",
                )

    def test_exhausted_run_budget_says_the_next_run_has_a_fresh_one(self):
        result = self.start(loop_review.MODE, ScriptedResult("committed"), budget=1)

        self.assert_stop_summary(
            result,
            "- agent sessions: 1\n- infrastructure retries: 0\n",
            "Stop reason: run budget exhausted before review",
            f"- #{CHILD}: handed to human review ({self.github.latest_comment_url(CHILD)}).",
            f"Next action:\n- #{CHILD} was handed to human review because the run budget was "
            f"exhausted: relabel it ready-for-agent, then run `ralph-kit run --review {PARENT}` "
            "again; each run starts with a fresh run budget.",
        )

    def test_exhausted_infrastructure_retries_give_their_reason_and_next_action(self):
        self.new_child_run(ci=("cancelled",))

        result = self.start(loop.MODE, ScriptedResult("committed"))

        self.assert_stop_summary(
            result,
            "- agent sessions: 1\n- infrastructure retries: 3\n",
            f"Stop reason: infrastructure retries exhausted for canonical CI after 3 retries: "
            f"{CANONICAL_CHECK} concluded cancelled",
            f"Next action:\n- #{CHILD} was handed to human review because CI evidence could not "
            "be obtained within the infrastructure retries: once GitHub Actions is healthy, "
            f"relabel it ready-for-agent, then run `ralph-kit run {PARENT}` again.",
        )

    def test_crash_names_the_exception_and_points_to_the_run_diagnostics(self):
        with self.assertRaises(ValueError):
            self.start(loop.MODE, ValueError("the adapter broke with token=hunter2"))

        directory = self.repository.diagnostics().relative_to(self.repository.path)
        summary = self.github.comments(PARENT)[-1]
        self.assertIn("- final outcome: crashed", summary)
        self.assertIn("Stop reason: ValueError: the adapter broke with token=[REDACTED]", summary)
        self.assertIn(f"Ralph crashed: inspect the run diagnostics in `{directory}`", summary)
        self.assertNotIn("hunter2", summary)

    def test_secrets_in_the_stop_reason_are_redacted(self):
        result = self.start(loop.MODE, RalphError("gh failed: token=hunter2 Bearer abc.def"))

        self.assert_stop_summary(
            result,
            "Stop reason: gh failed: token=[REDACTED] Bearer [REDACTED]",
            f"Next action:\n- Fix the stop reason, then run `ralph-kit run {PARENT}` again.\n"
            f"- #{CHILD} is labelled agent-working: implement and close it, or relabel it "
            f"ready-for-agent, then run `ralph-kit run {PARENT}` again.",
        )
        self.assertNotIn("hunter2", self.github.comments(PARENT)[-1])

    def test_rerun_command_keeps_the_configuration_path_and_drops_the_run_budget(self):
        (self.repository.path / "config").mkdir()
        git(self.repository.path, "mv", "ralph.toml", "config/ralph.toml")
        git(self.repository.path, "commit", "-m", "Move the Ralph configuration")
        self.sessions = ScriptedSessions(self.repository, ScriptedResult("blocked", "Stuck."))

        result = start_run(
            loop_review.MODE, self.repository, self.github, self.sessions,
            "--config", "config/ralph.toml", PARENT, 7,
        )

        self.assert_stop_summary(
            result, f"run `ralph-kit run --review --config config/ralph.toml {PARENT}` again."
        )

    def test_completed_run_summary_has_no_stop_details(self):
        result = self.start(loop.MODE, ScriptedResult("committed"))

        self.assert_implemented(result)
        self.assertTrue(self.github.comments(PARENT)[-1].endswith("- final outcome: completed"))
        self.assertNotIn("Next action", result.stdout + result.stderr)


class AgentSessionTestCase(VerificationTestCase):
    """Ralph's real session handling, with scripted engine processes in place of the CLIs."""

    def start_engine(self, mode, *results, budget=None, confirm=None):
        self.engine = ScriptedEngine(self.repository, *results)
        budget_argument = () if budget is None else (budget,)
        return start_run(
            mode, self.repository, self.github, self.engine, PARENT, *budget_argument,
            confirm=confirm,
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
                "agent structured output does not match the Ralph contract",
            ),
        ):
            with self.subTest(name):
                self.new_child_run(**run_options)

                result = self.start_engine(loop.MODE, *results)

                self.assert_stopped_without_publishing(result, reason)
                self.assertEqual(len(self.engine.calls), len(results))

    def test_no_changes_is_published_after_a_clean_independent_review(self):
        result = self.start_engine(
            loop_review.MODE, EngineResult("no_changes"), EngineResult("no_changes")
        )

        evidence = self.assert_implemented(result)
        self.assertEqual(len(self.engine.calls), 2)
        self.assertEqual(self.repository.head(), self.repository.base_sha)
        self.assertEqual(self.repository.pushed_head(), self.repository.base_sha)
        self.assertIn(f"`{self.repository.base_sha}`", evidence)

    def test_failed_session_with_a_commit_is_a_terminal_stop(self):
        result = self.start_engine(loop.MODE, EngineResult("failed", "The build tool crashed.", commit=True))

        self.assert_handed_to_human_review(
            result,
            "implementation stopped with failed: The build tool crashed.",
            "implementation reported failed: The build tool crashed.",
        )
        self.assertEqual(len(self.engine.calls), 1)
        self.assertIsNone(self.github.pull_request())

    def test_claimed_commit_without_a_git_commit_hands_the_child_to_human_review(self):
        result = self.start_engine(loop.MODE, EngineResult("committed", commit=False))

        self.assert_handed_to_human_review(
            result,
            "implementation claimed committed without creating a Git commit",
            "Ralph stopped because implementation claimed committed without creating a Git commit.",
        )
        self.assertEqual(len(self.engine.calls), 1)
        self.assertIsNone(self.github.pull_request())
        with self.assertRaises(subprocess.CalledProcessError):
            self.repository.pushed_head()

    def test_dirty_agent_output_cannot_be_published_and_keeps_its_evidence(self):
        result = self.start_engine(loop.MODE, EngineResult("committed", dirty=True))

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

                result = self.start_engine(loop_review.MODE, session)

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
        result = self.start_engine(loop.MODE, EngineResult("committed", returncode=1))

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

                result = self.start_engine(loop.MODE, dataclasses.replace(session, returncode=1))

                self.assert_stopped_without_publishing(
                    result, "implementation exited non-zero without a committed outcome"
                )
                self.assertEqual(len(self.engine.calls), 1)

    def test_claude_code_failure_without_a_result_reports_its_exit_code_and_stderr(self):
        self.new_child_run(engines={"implementation": "claude"})

        result = self.start_engine(
            loop_review.MODE,
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

                result = self.start_engine(loop.MODE, *results)

                self.assert_handed_to_human_review(
                    result,
                    f"{role} stopped with blocked: {decision}",
                    f"Ralph stopped because {role} reported blocked: {decision}",
                )
                self.assertEqual(len(self.engine.calls), len(results))

    def test_findings_are_preserved_in_full_and_redacted_for_correction_and_handoff(self):
        details = "Section details. " * 100
        finding = f"First requirement.\n{details}\nLast requirement. token=fixture-secret"

        result = self.start_engine(
            loop.MODE,
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
                    loop_review.MODE, EngineResult("committed", summary), EngineResult("no_changes")
                )

                self.assert_implemented(result)
                implementation, _review = self.repository.session_records()
                self.assertEqual(implementation["structured_result"]["summary"], recorded)
                self.assertIn(f"Previous session summary: {recorded}\n", self.engine.calls[1].prompt)

    def test_each_session_receives_the_previous_session_summary(self):
        self.new_child_run(local_gate_results=("fail", "pass"), ci=("failure", "success"))

        result = self.start_engine(
            loop.MODE,
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
                    loop_review.MODE,
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

        result = self.start_engine(loop.MODE, EngineResult("committed"), EngineResult("committed"))

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

        result = self.start_engine(loop_review.MODE, EngineResult("committed"), EngineResult("no_changes"))

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
    ("result breaking the Ralph contract", {},
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
        marker = "🏁 Ralph summary"
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

                result = self.start_engine(loop.MODE, failure, EngineResult("committed"))

                self.assert_implemented(result)
                failed, replay = self.engine.calls
                self.assertEqual(failed.engine, engines.get("implementation", "codex"))
                self.assert_backup_call(replay)
                # The same request: Claude Code only spells the implement skill differently.
                self.assertEqual(replay.prompt, failed.prompt.replace("$implement ", "/implement ", 1))
                self.assertIn("- agent sessions: 2\n", result.stdout)

    def test_replay_starts_from_the_head_the_failed_session_started_from(self):
        result = self.start_engine(
            loop.MODE,
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
            loop_review.MODE,
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
            budget=10,
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
            loop_review.MODE,
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
            loop_review.MODE,
            EngineResult("committed"),
            EngineResult(writes_result=False, commit=False),
            EngineResult("no_changes"),
        )

        self.assert_implemented(result)
        self.assertIn("🏁 Ralph summary", self.github.pull_request().comments[-1])
        self.assert_backup_line(
            result,
            f"- backup: Claude Code Opus - High took over after a Codex engine failure "
            f"(review session, child #{CHILD})",
        )

    def test_run_summary_without_a_switch_has_no_backup_line(self):
        for backup in (True, False):
            with self.subTest(backup=backup):
                self.new_child_run(backup=backup)

                result = self.start_engine(loop.MODE, EngineResult("committed"))

                self.assert_implemented(result)
                for summary in self.published_summaries(result):
                    self.assertIn("- infrastructure retries: 0\n- total duration: ", summary)
                    self.assertNotIn("- backup:", summary)

    def test_switch_is_announced_and_recorded_in_the_run_diagnostics(self):
        result = self.start_engine(
            loop.MODE,
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
            loop.MODE,
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
            loop.MODE,
            EngineResult(writes_result=False, commit=False),
            EngineResult(writes_result=False, commit=False),
        )
        self.assertEqual(self.github.labels(CHILD), {"agent-working"})

        result = self.start_engine(
            loop.MODE, EngineResult("committed"), confirm=lambda question: True
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

                result = self.start_engine(loop.MODE, *results)

                self.assertEqual(result.exit_code, expected_exit, result.stderr)
                self.assertEqual(self.engine.engines(), ["codex"] * len(results))
                self.assertNotIn("backup", result.stdout)

    def test_nonzero_exit_with_a_valid_committed_result_is_accepted_without_switching(self):
        result = self.start_engine(loop.MODE, EngineResult("committed", returncode=1))

        self.assert_implemented(result)
        self.assertEqual(self.engine.engines(), ["codex"])
        self.assertNotIn("backup", result.stdout)

    def test_failed_session_counts_toward_the_run_budget(self):
        self.new_child_run()

        result = self.start_engine(
            loop_review.MODE,
            EngineResult(writes_result=False, commit=False),
            EngineResult("committed"),
            budget=2,
        )

        self.assert_handed_to_human_review(
            result,
            "run budget exhausted before review",
            "run budget of 2 agent sessions was exhausted after 2 sessions",
        )
        self.assertEqual(self.engine.engines(), ["codex", "claude"])

    def test_engine_failure_on_the_last_budgeted_session_exhausts_the_run_budget_instead_of_replaying(self):
        result = self.start_engine(
            loop.MODE,
            EngineResult("blocked", "Usage limit.", returncode=1, commit=True, dirty=True),
            budget=1,
        )

        self.assert_handed_to_human_review(
            result,
            "run budget exhausted before implementation",
            "Ralph stopped before implementation because the run budget of 1 agent sessions "
            "was exhausted after 1 sessions.",
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
            loop.MODE,
            EngineResult("committed"),
            EngineResult(writes_result=False, commit=False),
            EngineResult("committed"),
            EngineResult("committed"),
            EngineResult("committed"),
        )

        self.assert_implemented(result)
        self.assertEqual(self.engine.engines(), ["codex", "codex", "claude", "claude", "claude"])
        self.assertIn("Repair the failing local gate", self.engine.calls[2].prompt)


if __name__ == "__main__":
    unittest.main()
