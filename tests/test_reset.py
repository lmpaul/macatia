"""`ralph-kit reset` through its entry point: the fake GitHub, a scripted confirmer, and a
temporary checkout with a bare `origin`."""

import contextlib
import hashlib
import io
import json
import pathlib
import re
import sys
import unittest


TESTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "src"))
sys.path.insert(0, str(TESTS))

from ralph_kit import cli, loop_core, reset  # noqa: E402
from ralph_kit.commands import RalphError  # noqa: E402
from run_support import (  # noqa: E402
    CommitPull,
    FakeGitHub,
    RunInterrupted,
    RunRepository,
    ScriptedResult,
    ScriptedSessions,
    child_base_marker,
    git,
    implementation_evidence,
    start_run,
)
from test_close import ScriptedConfirmer  # noqa: E402


PARENT = 69


def sha(name):
    """A distinct 40-hex commit SHA for a name."""
    return hashlib.sha1(name.encode()).hexdigest()


def reset_markers(bodies):
    """The fields of each reset marker among comment bodies, in order."""
    return [
        json.loads(match.group(1))
        for body in bodies
        if (match := re.search(re.escape(loop_core.RESET_MARKER_PREFIX) + r"(\{.*?\}) -->", body))
    ]


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.repository = RunRepository(self)
        self.github = FakeGitHub()

    def add_parent(self, *, labels=("work-started",), **options):
        return self.github.add_issue(PARENT, labels=labels, **options)

    def add_child(self, number, *, labels=("implemented",), comments=None, **options):
        """A child; by default with implementation evidence whose verified head is not merged."""
        if comments is None:
            comments = [self.evidence(number)]
        return self.github.add_issue(number, parent=PARENT, labels=labels, comments=comments, **options)

    def evidence(self, number, *, merged=False):
        verified_head = sha(f"verified-{number}")
        self.github.on_branch[verified_head] = merged
        return implementation_evidence(loop_core.STANDARD_RUN, sha(f"implementation-{number}"), verified_head)

    def reset(self, *arguments, confirm=None):
        """Run `ralph-kit reset`; confirm=None stands for no terminal."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = reset.run(
                    list(arguments),
                    github=lambda _root, _repository: self.github,
                    confirm=confirm,
                    repository_root=self.repository.path,
                )
            except SystemExit as error:  # argparse usage errors.
                code = error.code
        self.stdout, self.stderr = stdout.getvalue(), stderr.getvalue()
        return code

    def snapshot(self):
        return json.dumps(self.github.issues, sort_keys=True)

    def assert_reset_child(self, number, previous_state):
        self.assertEqual(self.github.labels(number) & self.lifecycle(), {"ready-for-agent"})
        self.assertEqual(reset_markers(self.github.comments(number))[-1], {"previous_state": previous_state})

    @staticmethod
    def lifecycle():
        return {"ready-for-agent", "agent-working", "implemented", "human-review"}

    # Labels and reset markers.

    def test_resets_each_child_then_the_parent_with_its_recap_last(self):
        self.add_parent(labels=("work-started", "human-review", "needs-triage"))
        self.add_child(70)
        self.add_child(71, labels=("human-review",))
        self.add_child(72, labels=("agent-working", "needs-info"))
        writes = []
        self.github.after_comment = lambda number, body: writes.append(("comment", number))
        self.github.after_label_edit = lambda number, add, remove: writes.append(("labels", number))

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

        self.assert_reset_child(70, "implemented")
        self.assert_reset_child(71, "human_review")
        self.assert_reset_child(72, "agent_working")
        self.assertIn("needs-info", self.github.labels(72))
        self.assertEqual(self.github.labels(PARENT), {"ready-for-agent", "needs-triage"})
        self.assertEqual(writes[-2:], [("labels", PARENT), ("comment", PARENT)])
        self.assertEqual(
            reset_markers(self.github.comments(PARENT)),
            [{"children": [
                {"number": 70, "previous_state": "implemented"},
                {"number": 71, "previous_state": "human_review"},
                {"number": 72, "previous_state": "agent_working"},
            ]}],
        )
        recap = self.github.comments(PARENT)[-1]
        self.assertIn("#70, was `implemented`", recap)
        self.assertIn("#71, was `human-review`", recap)

    def test_children_without_a_lifecycle_label_ready_or_closed_are_untouched(self):
        self.add_parent()
        self.add_child(70)
        self.add_child(71, labels=(), comments=[])
        self.add_child(72, labels=("ready-for-agent",), comments=[])
        self.add_child(73, state="closed", labels=("implemented",))
        before = {number: dict(self.github.issues[number]) for number in (71, 72, 73)}

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

        for number in (71, 72, 73):
            self.assertEqual(self.github.issues[number], before[number])
        [recap] = reset_markers(self.github.comments(PARENT))
        self.assertEqual(recap, {"children": [{"number": 70, "previous_state": "implemented"}]})

    def test_a_parent_marked_only_work_started_is_resettable(self):
        self.add_parent(labels=("work-started",))
        self.add_child(70, labels=("ready-for-agent",), comments=[])

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

        self.assertEqual(self.github.labels(PARENT), {"ready-for-agent"})
        self.assertEqual(reset_markers(self.github.comments(PARENT)), [{"children": []}])
        self.assertEqual(self.github.comments(70), [])

    def test_a_standalone_issue_keeps_its_marker_and_gets_one_recap_reset_marker(self):
        self.github.add_issue(80, labels=("implemented", "standalone"), comments=[self.evidence(80)])

        self.assertEqual(self.reset("80", "--yes"), 0, self.stderr)

        self.assertEqual(self.github.labels(80), {"ready-for-agent", "standalone"})
        self.assertEqual(reset_markers(self.github.comments(80)), [{"previous_state": "implemented"}])
        self.assertIn("standalone issue", self.github.comments(80)[-1])

    # The plan and its confirmation.

    def test_the_plan_lists_every_label_change_before_anything_is_written(self):
        self.add_parent(labels=("work-started", "human-review"))
        self.add_child(70)
        self.add_child(71, labels=("human-review",))
        before = self.snapshot()
        seen = []

        def confirm(question):
            seen.append(self.snapshot())
            return ScriptedConfirmer(True)(question)

        self.assertEqual(self.reset(str(PARENT), confirm=confirm), 0, self.stderr)

        self.assertEqual(seen, [before])
        self.assertIn("#70 Issue 70: implemented → ready-for-agent", self.stdout)
        self.assertIn("#71 Issue 71: human-review → ready-for-agent", self.stdout)
        self.assertIn(f"#{PARENT}: -human-review, -work-started, +ready-for-agent", self.stdout)

    def test_declining_or_ctrl_c_writes_nothing(self):
        for answer, code in ((False, 1), (KeyboardInterrupt(), 130)):
            with self.subTest(answer=answer):
                self.github = FakeGitHub()
                self.add_parent()
                self.add_child(70)
                before = self.snapshot()

                self.assertEqual(self.reset(str(PARENT), confirm=ScriptedConfirmer(answer)), code)

                self.assertEqual(self.snapshot(), before)
                self.assertIn("Nothing reset", self.stderr)

    def test_without_a_terminal_or_yes_the_reset_is_refused(self):
        self.add_parent()
        self.add_child(70)
        before = self.snapshot()

        self.assertEqual(self.reset(str(PARENT)), 1)

        self.assertIn("--yes", self.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_an_agent_working_child_is_warned_about_and_reset_once_confirmed(self):
        self.add_parent()
        self.add_child(70, labels=("agent-working",), comments=[])
        confirmer = ScriptedConfirmer(True)

        self.assertEqual(self.reset(str(PARENT), confirm=confirmer), 0, self.stderr)

        self.assertIn("⚠️  #70 is agent-working: a run may still be working on it", self.stdout)
        self.assertEqual(confirmer.questions, [f"Reset #{PARENT}?"])
        self.assert_reset_child(70, "agent_working")

    # Refusals.

    def assert_refused(self, number, *reasons):
        before = self.snapshot()
        self.assertEqual(self.reset(str(number), "--yes"), 1)
        for reason in reasons:
            self.assertIn(reason, self.stderr)
        self.assertIn("Nothing reset", self.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_a_nonexistent_issue_is_refused(self):
        self.assertEqual(self.reset("404", "--yes"), 1)
        self.assertIn("Cannot read #404, nothing reset", self.stderr)

    def test_an_untrusted_run_target_is_refused(self):
        self.add_parent(author="intruder")
        self.add_child(70)
        self.assert_refused(PARENT, f"#{PARENT} is authored by @intruder")

    def test_a_sub_issue_is_refused_naming_its_parent(self):
        self.add_parent()
        self.add_child(70)
        self.assert_refused(70, f"#70 is a sub-issue of #{PARENT}", f"reset #{PARENT} instead")

    def test_a_closed_run_target_is_refused(self):
        self.add_parent(state="closed")
        self.add_child(70)
        self.assert_refused(PARENT, f"#{PARENT} is closed")

    def test_a_run_target_with_a_merged_child_is_refused(self):
        for name, merge in (
            ("by ancestry", lambda head: self.github.on_branch.__setitem__(head, True)),
            ("by a merged pull request", lambda head: self.github.commit_pulls.__setitem__(
                head, [CommitPull(12, sha("merge"))]
            )),
        ):
            with self.subTest(name):
                self.github = FakeGitHub()
                self.add_parent()
                self.add_child(70)
                self.add_child(71, state="closed", labels=())
                merge(sha("verified-71"))
                self.assert_refused(PARENT, "#71 is merged", "never undoes merged work")

    def test_a_merged_standalone_issue_is_refused(self):
        self.github.add_issue(80, labels=("implemented",), comments=[self.evidence(80, merged=True)])
        self.assert_refused(80, "#80 is merged")

    def test_a_run_target_without_the_trace_of_a_run_is_refused(self):
        self.add_parent(labels=("ready-for-agent",))
        self.add_child(70, labels=("ready-for-agent",), comments=[])
        self.add_child(71, state="closed", labels=("implemented",))
        self.github.add_issue(80, labels=("ready-for-agent", "standalone"))
        self.assert_refused(PARENT, f"#{PARENT} is not resettable")
        self.assert_refused(80, "#80 is not resettable")

    # Interruptions, errors and exit codes.

    def test_a_rerun_after_an_interruption_between_children_and_parent_finishes_the_reset(self):
        self.add_parent(labels=("work-started", "human-review"))
        self.add_child(70)
        self.add_child(71, labels=("human-review",))

        def interrupt(number, add, remove):
            if number == 71:
                raise KeyboardInterrupt

        self.github.after_label_edit = interrupt

        self.assertEqual(self.reset(str(PARENT), "--yes"), 130)

        self.assertIn(f"Rerun `ralph-kit reset {PARENT}` to finish", self.stderr)
        self.assertEqual(self.github.labels(PARENT), {"work-started", "human-review"})
        self.assertEqual(self.github.comments(PARENT), [])
        self.github.after_label_edit = None

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

        self.assert_reset_child(70, "implemented")
        self.assert_reset_child(71, "human_review")
        self.assertEqual(len(reset_markers(self.github.comments(70))), 1)
        self.assertEqual(len(reset_markers(self.github.comments(71))), 1)
        self.assertEqual(self.github.labels(PARENT), {"ready-for-agent"})
        self.assertEqual(
            reset_markers(self.github.comments(PARENT)),
            [{"children": [
                {"number": 70, "previous_state": "implemented"},
                {"number": 71, "previous_state": "human_review"},
            ]}],
        )

    def test_a_rerun_after_the_parent_labels_but_before_its_recap_finishes_the_reset(self):
        self.add_parent()
        self.add_child(70)
        self.github.interrupt_after_comment_on = 70

        with self.assertRaises(RunInterrupted):
            self.reset(str(PARENT), "--yes")
        self.github.edit_issue_labels(70, add="ready-for-agent", remove=["implemented"])
        self.github.edit_issue_labels(PARENT, add="ready-for-agent", remove=["work-started"])

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

        self.assertIn("already reset", self.stdout)
        self.assertEqual(len(reset_markers(self.github.comments(70))), 1)
        self.assertEqual(
            reset_markers(self.github.comments(PARENT)),
            [{"children": [{"number": 70, "previous_state": "implemented"}]}],
        )

    def test_the_recap_lists_only_children_reset_since_the_previous_recap(self):
        self.add_parent()
        self.add_child(70)
        self.add_child(71, labels=("ready-for-agent",), comments=[])
        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)
        self.github.edit_issue_labels(PARENT, add="work-started", remove=[])
        self.github.edit_issue_labels(71, add="implemented", remove=["ready-for-agent"])

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

        self.assertEqual(
            reset_markers(self.github.comments(PARENT))[-1],
            {"children": [{"number": 71, "previous_state": "implemented"}]},
        )

    def test_a_github_error_exits_1_and_a_rerun_finishes_the_reset(self):
        self.add_parent()
        self.add_child(70)
        self.github.label_edit_error = RalphError("command failed (gh issue edit 70): HTTP 502")

        self.assertEqual(self.reset(str(PARENT), "--yes"), 1)

        self.assertIn("HTTP 502", self.stderr)
        self.assertIn(f"Rerun `ralph-kit reset {PARENT}` to finish", self.stderr)
        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)
        self.assert_reset_child(70, "implemented")
        self.assertEqual(len(reset_markers(self.github.comments(70))), 1)

    def test_usage_errors_exit_2(self):
        self.assertEqual(self.reset(), 2)
        self.assertEqual(self.reset("not-a-number"), 2)
        self.assertEqual(self.reset(str(PARENT), "--unknown"), 2)

    def test_the_command_line_lists_and_dispatches_reset(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["--help"]), 0)
        self.assertIn("reset [--yes] [--config PATH] ISSUE", stdout.getvalue())
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as usage,
        ):
            cli.main(["reset"])
        self.assertEqual(usage.exception.code, 2)

    def test_the_configuration_path_is_honoured_and_named_in_the_rerun(self):
        (self.repository.path / "other.toml").write_text(
            (self.repository.path / "ralph.toml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.add_parent()
        self.add_child(70)
        self.github.label_edit_error = RalphError("HTTP 502")

        self.assertEqual(self.reset("--config", "other.toml", str(PARENT), "--yes"), 1)

        self.assertIn(f"ralph-kit reset --config other.toml {PARENT}", self.stderr)

    # A run after the reset.

    def test_a_run_after_the_reset_implements_the_children_from_scratch(self):
        prior = self.repository.head()
        (self.repository.path / "prior.txt").write_text("prior\n", encoding="utf-8")
        git(self.repository.path, "add", "prior.txt")
        git(self.repository.path, "commit", "-m", "Prior child")
        verified_head = self.repository.head()
        self.add_parent()
        self.add_child(70, comments=[
            child_base_marker(prior),
            implementation_evidence(loop_core.STANDARD_RUN, verified_head),
        ])

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)
        sessions = ScriptedSessions(self.repository, ScriptedResult("committed"))
        result = start_run(self.repository, self.github, sessions, PARENT)

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual([request.child for request in sessions.requests], [70])
        self.assertEqual(self.github.labels(70) & self.lifecycle(), {"implemented"})
        [*_, child_base] = [
            json.loads(match.group(1))["child_base"]
            for body in self.github.comments(70)
            if (match := re.search(re.escape(loop_core.CHILD_BASE_MARKER_PREFIX) + r"(\{.*?\}) -->", body))
        ]
        self.assertEqual(child_base, verified_head)


if __name__ == "__main__":
    unittest.main()
