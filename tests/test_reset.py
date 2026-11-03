"""`macatia reset` through its entry point: the fake GitHub, a scripted confirmer, and a
temporary checkout with a bare `origin`."""

import contextlib
import hashlib
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock


TESTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "src"))
sys.path.insert(0, str(TESTS))

from macatia import check, cli, loop_core, reset  # noqa: E402
from macatia.commands import MacatiaError  # noqa: E402
from run_support import (  # noqa: E402
    BRANCH,
    TRUSTED_LOGIN,
    CommitPull,
    FakeGitHub,
    PullRequest,
    RunInterrupted,
    RunRepository,
    ScriptedResult,
    ScriptedSessions,
    child_base_marker,
    git,
    implementation_evidence,
    legacy_implementation_evidence,
    reset_marker,
    start_run,
)
from test_close import ScriptedConfirmer, ScriptedPicker  # noqa: E402


PARENT = 69
RUN_BRANCH = f"macatia/issue-{PARENT}"


def sha(name):
    """A distinct 40-hex commit SHA for a name."""
    return hashlib.sha1(name.encode()).hexdigest()


def trusted(body):
    return {"author": {"login": TRUSTED_LOGIN}, "body": body}


def reset_markers(bodies):
    """The fields of each reset marker among comment bodies, in order."""
    return [marker for body in bodies if (marker := reset.reset_marker_comment(body)) is not None]


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

    def reset(self, *arguments, confirm=None, choose=None):
        """Run `macatia reset`; confirm=None and choose=None stand for no terminal."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = reset.run(
                    list(arguments),
                    github=lambda _root, _repository: self.github,
                    confirm=confirm,
                    choose_run_target=choose,
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

    def add_run_branch(self, *, checked_out=False, pushed=True):
        """The run branch with one commit of its own, on origin unless `pushed` is False, left
        checked out when `checked_out`; returns its head."""
        git(self.repository.path, "switch", "--create", RUN_BRANCH)
        (self.repository.path / "run.txt").write_text("run\n", encoding="utf-8")
        git(self.repository.path, "add", "run.txt")
        git(self.repository.path, "commit", "-m", "Run commit")
        if pushed:
            git(self.repository.path, "push", "--set-upstream", "origin", RUN_BRANCH)
        head = self.repository.head()
        if not checked_out:
            git(self.repository.path, "switch", BRANCH)
        return head

    def add_pull_request(self, *, head=RUN_BRANCH, title=f"Implement Macatia parent #{PARENT}", base="main"):
        number = len(self.github.pull_requests) + 12
        self.github.pull_requests.append(PullRequest(number, base, head, title, "Cumulative."))
        return number

    def branches(self):
        """The run branch where it is: in the checkout and on origin."""
        return {
            place
            for place, repository in (("checkout", self.repository.path), ("origin", self.repository.origin))
            if git(repository, "branch", "--list", RUN_BRANCH)
        }

    def checked_out(self):
        return git(self.repository.path, "symbolic-ref", "--short", "HEAD")

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
        self.assertIn("-implemented, +ready-for-agent", self.stdout)

    # The cumulative draft PR and the run branch.

    def test_closes_the_pull_request_and_deletes_the_run_branch_before_any_label_change(self):
        self.add_parent()
        self.add_child(70)
        self.add_run_branch()
        pull = self.add_pull_request()
        writes = []
        self.github.after_pull_request_close = lambda number: writes.append(("close", number, self.branches()))
        self.github.after_comment = lambda number, body: writes.append(("comment", number))
        self.github.after_label_edit = lambda number, add, remove: writes.append(("labels", number))

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

        self.assertEqual(writes[0], ("close", pull, {"checkout", "origin"}))
        self.assertEqual(self.github.pull_requests[0].state, "closed")
        [comment] = self.github.pull_requests[0].comments
        self.assertIn(f"reset of #{PARENT} closed", comment)
        self.assertEqual(self.branches(), set())
        self.assertEqual(self.checked_out(), BRANCH)
        self.assert_reset_child(70, "implemented")

    def test_a_checked_out_run_branch_is_deleted_after_switching_to_the_base_branch(self):
        self.add_parent()
        self.add_child(70)
        self.add_run_branch(checked_out=True)
        self.add_pull_request()

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

        self.assertIn(", switching to main first", self.stdout)
        self.assertEqual(self.checked_out(), "main")
        self.assertEqual(self.branches(), set())

    def test_a_dirty_worktree_on_the_run_branch_is_refused_before_anything_is_written(self):
        self.add_parent()
        self.add_child(70)
        self.add_run_branch(checked_out=True)
        self.add_pull_request()
        (self.repository.path / "run.txt").write_text("edited\n", encoding="utf-8")

        self.assert_refused(PARENT, f"the worktree is dirty while {RUN_BRANCH} is checked out")

        self.assertEqual(self.github.pull_requests[0].state, "open")
        self.assertEqual(self.branches(), {"checkout", "origin"})
        self.assertEqual(self.checked_out(), RUN_BRANCH)

    def test_a_pull_request_from_another_branch_is_closed_and_its_branch_kept(self):
        self.add_parent()
        self.add_child(70)
        pull = self.add_pull_request(head=BRANCH)
        git(self.repository.path, "push", "origin", BRANCH)
        self.add_pull_request(title="Implement Macatia parent #70")
        self.add_pull_request(base="release")

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

        self.assertIn(f"⚠️  PR #{pull} comes from {BRANCH}: it is closed, but {BRANCH} is kept", self.stdout)
        self.assertEqual([pull.state for pull in self.github.pull_requests], ["closed", "open", "open"])
        self.assertTrue(git(self.repository.path, "branch", "--list", BRANCH))
        self.assertTrue(git(self.repository.origin, "branch", "--list", BRANCH))
        self.assertEqual(reset_markers(self.github.comments(PARENT))[-1]["closed_pull_requests"], [pull])

    def test_a_run_branch_without_a_pull_request_is_deleted_after_a_warning(self):
        self.add_parent()
        self.add_child(70)
        head = self.add_run_branch()

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

        self.assertIn(
            f"⚠️  {RUN_BRANCH} has no open pull request to restore it from: once deleted, only its "
            "head SHA in the recap and your reflog can recover its commits",
            self.stdout,
        )
        self.assertEqual(self.branches(), set())
        recap = reset_markers(self.github.comments(PARENT))[-1]
        self.assertEqual(recap["deleted_branch"], {"name": RUN_BRANCH, "head": head})
        self.assertNotIn("closed_pull_requests", recap)

    def test_the_plan_lists_the_pull_request_and_branches_and_the_recap_names_them(self):
        self.add_parent()
        self.add_child(70)
        head = self.add_run_branch()
        pull = self.add_pull_request()

        self.assertEqual(self.reset(str(PARENT), confirm=ScriptedConfirmer(True)), 0, self.stderr)

        plan = self.stdout.split("✓")[0]
        self.assertIn(f"→ close PR #{pull} Implement Macatia parent #{PARENT} from {RUN_BRANCH}, with a comment", plan)
        self.assertIn(f"→ delete {RUN_BRANCH} on origin at {head}, then in this checkout at {head}", plan)
        self.assertNotIn("⚠️", plan)
        self.assertIn(f"✓ closed PR #{pull}", self.stdout)
        self.assertIn(f"✓ deleted {RUN_BRANCH} at {head}", self.stdout)
        [recap] = self.github.comments(PARENT)
        self.assertEqual(reset_markers([recap]), [{
            "children": [{"number": 70, "previous_state": "implemented"}],
            "closed_pull_requests": [pull],
            "deleted_branch": {"name": RUN_BRANCH, "head": head},
        }])
        self.assertIn(f"It closed PR #{pull} and deleted `{RUN_BRANCH}` at `{head}`.", recap)

    def test_a_standalone_recap_names_the_closed_pull_request_and_the_deleted_branch(self):
        self.github.add_issue(PARENT, labels=("implemented", "standalone"), comments=[self.evidence(PARENT)])
        head = self.add_run_branch()
        pull = self.add_pull_request(title=f"Implement Macatia issue #{PARENT}")

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

        self.assertEqual(reset_markers(self.github.comments(PARENT)), [{
            "previous_state": "implemented",
            "closed_pull_requests": [pull],
            "deleted_branch": {"name": RUN_BRANCH, "head": head},
        }])
        self.assertEqual(self.branches(), set())

    def test_a_run_target_whose_only_trace_is_a_run_branch_or_a_pull_request_is_resettable(self):
        for name, trace in (
            ("run branch", self.add_run_branch),
            ("pull request", lambda: self.add_pull_request(head=BRANCH)),
        ):
            with self.subTest(name):
                self.repository = RunRepository(self)
                self.github = FakeGitHub()
                self.add_parent(labels=("ready-for-agent",))
                self.add_child(70, labels=("ready-for-agent",), comments=[])
                trace()

                self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

                self.assertEqual(self.branches(), set())
                self.assertTrue(all(pull.state == "closed" for pull in self.github.pull_requests))
                self.assertEqual(len(reset_markers(self.github.comments(PARENT))), 1)

    def test_a_rerun_after_an_interruption_between_the_pull_request_and_the_branch_finishes(self):
        self.add_parent()
        self.add_child(70)
        head = self.add_run_branch(checked_out=True)
        pull = self.add_pull_request()

        def interrupt(number):
            raise KeyboardInterrupt

        self.github.after_pull_request_close = interrupt

        self.assertEqual(self.reset(str(PARENT), "--yes"), 130)

        self.assertIn(f"Rerun `macatia reset {PARENT}` to finish", self.stderr)
        self.assertEqual(self.branches(), {"checkout", "origin"})
        self.assertEqual(self.github.labels(70), {"implemented"})
        self.github.after_pull_request_close = None

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

        self.assertEqual(self.branches(), set())
        self.assertEqual(self.checked_out(), "main")
        self.assertEqual(len(self.github.pull_requests[0].comments), 1)
        self.assertIn(f"✓ PR #{pull}: already closed by this reset", self.stdout)
        self.assertNotIn("no open pull request to restore it from", self.stdout)
        self.assert_reset_child(70, "implemented")
        self.assertEqual(reset_markers(self.github.comments(PARENT)), [{
            "children": [{"number": 70, "previous_state": "implemented"}],
            "closed_pull_requests": [pull],
            "deleted_branch": {"name": RUN_BRANCH, "head": head},
        }])

    def test_a_rerun_after_the_branch_was_deleted_still_recaps_it(self):
        self.add_parent()
        self.add_child(70)
        head = self.add_run_branch()
        pull = self.add_pull_request()
        self.github.interrupt_after_comment_on = 70

        with self.assertRaises(RunInterrupted):
            self.reset(str(PARENT), "--yes")
        self.assertEqual(self.branches(), set())

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

        self.assertEqual(reset_markers(self.github.comments(PARENT)), [{
            "children": [{"number": 70, "previous_state": "implemented"}],
            "closed_pull_requests": [pull],
            "deleted_branch": {"name": RUN_BRANCH, "head": head},
        }])

    def test_a_pull_request_closed_before_the_previous_recap_is_not_recapped_again(self):
        self.add_parent()
        self.add_child(70)
        self.add_run_branch()
        self.add_pull_request()
        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)
        self.github.edit_issue_labels(PARENT, add="work-started", remove=[])

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

        self.assertEqual(reset_markers(self.github.comments(PARENT))[-1], {"children": []})

    def test_a_git_error_deleting_the_branch_exits_1_before_any_label_change(self):
        self.add_parent()
        self.add_child(70)
        self.add_run_branch()
        hook = self.repository.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho rejected >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

        self.assertEqual(self.reset(str(PARENT), "--yes"), 1)

        self.assertIn("rejected", self.stderr)
        self.assertIn(f"Rerun `macatia reset {PARENT}` to finish", self.stderr)
        self.assertEqual(self.github.labels(70), {"implemented"})
        hook.unlink()
        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)
        self.assertEqual(self.branches(), set())

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

    # The run worktree.

    def add_run_worktree(self):
        """The run branch with one commit of its own, pushed, checked out in its run worktree
        with an ignored file; returns the run worktree's path."""
        path = self.repository.path.parent / "repository.worktrees" / f"issue-{PARENT}"
        git(self.repository.path, "worktree", "add", "--quiet", "-b", RUN_BRANCH, str(path))
        (path / "run.txt").write_text("run\n", encoding="utf-8")
        git(path, "add", "run.txt")
        git(path, "commit", "-m", "Run commit")
        git(path, "push", "--set-upstream", "origin", RUN_BRANCH)
        (path / ".gitignore").write_text(".macatia/\n", encoding="utf-8")
        git(path, "add", ".gitignore")
        git(path, "commit", "-m", "Ignore run diagnostics")
        git(path, "push")
        (path / ".macatia").mkdir()
        (path / ".macatia" / "diagnostics.txt").write_text("diagnostics\n", encoding="utf-8")
        return path

    def test_the_run_worktree_is_listed_then_removed_with_its_ignored_files_before_the_run_branch(self):
        self.add_parent()
        self.add_child(70)
        path = self.add_run_worktree()
        self.add_pull_request()
        removed = []
        real_command = reset.command

        def command(root, arguments, **options):
            if arguments[:3] == ["git", "push", "origin"]:
                removed.append(path.exists())
            return real_command(root, arguments, **options)

        with mock.patch.object(reset, "command", command):
            self.assertEqual(self.reset(str(PARENT), confirm=ScriptedConfirmer(True)), 0, self.stderr)

        plan = self.stdout.split("✓")[0]
        self.assertIn(f"→ remove the run worktree of {RUN_BRANCH} at {path}, with its ignored files", plan)
        self.assertIn(f"✓ removed the run worktree of {RUN_BRANCH} at {path}", self.stdout)
        self.assertEqual(removed, [False])
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), git(self.repository.path, "worktree", "list"))
        self.assertEqual(self.branches(), set())
        self.assertEqual(self.checked_out(), BRANCH)
        self.assert_reset_child(70, "implemented")

    def test_a_dirty_run_worktree_is_refused_among_the_other_reasons_before_anything_is_written(self):
        self.add_parent()
        self.add_child(70)
        self.add_child(71, labels=("implemented",), comments=[self.evidence(71, merged=True)])
        path = self.add_run_worktree()
        self.add_pull_request()
        (path / "run.txt").write_text("edited\n", encoding="utf-8")

        self.assert_refused(
            PARENT, "#71 is merged", f"the run worktree of {RUN_BRANCH} at {path} is dirty"
        )

        self.assertEqual(self.github.pull_requests[0].state, "open")
        self.assertTrue(path.exists())
        self.assertEqual(self.branches(), {"checkout", "origin"})

    def test_an_untracked_file_in_the_run_worktree_makes_it_dirty(self):
        self.add_parent()
        self.add_child(70)
        path = self.add_run_worktree()
        (path / "notes.txt").write_text("notes\n", encoding="utf-8")

        self.assert_refused(PARENT, f"the run worktree of {RUN_BRANCH} at {path} is dirty")

        self.assertTrue((path / "notes.txt").exists())

    def test_a_reset_started_from_inside_the_run_worktree_is_refused(self):
        self.add_parent()
        self.add_child(70)
        path = self.add_run_worktree()
        self.add_pull_request()
        (path / "run.txt").write_text("edited\n", encoding="utf-8")
        self.repository.path = path

        self.assert_refused(
            PARENT,
            f"the reset was started from inside the run worktree of {RUN_BRANCH} at {path}",
            "run it from outside the run worktree",
            f"the run worktree of {RUN_BRANCH} at {path} is dirty",
        )

        self.assertTrue(path.exists())
        self.assertEqual(self.github.pull_requests[0].state, "open")

    def test_a_locked_run_worktree_is_refused_before_anything_is_written(self):
        self.add_parent()
        self.add_child(70)
        path = self.add_run_worktree()
        self.add_pull_request()
        git(self.repository.path, "worktree", "lock", str(path))

        self.assert_refused(
            PARENT,
            f"the run worktree of {RUN_BRANCH} at {path} is locked",
            f"git worktree unlock {path}",
        )

        self.assertEqual(self.github.pull_requests[0].state, "open")
        self.assertTrue(path.exists())

    def test_a_run_worktree_whose_directory_was_deleted_is_removed_with_its_run_branch(self):
        self.add_parent()
        self.add_child(70)
        path = self.add_run_worktree()
        shutil.rmtree(path)

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

        self.assertIn(f"✓ removed the run worktree of {RUN_BRANCH} at {path}", self.stdout)
        self.assertNotIn(str(path), git(self.repository.path, "worktree", "list"))
        self.assertEqual(self.branches(), set())

        self.add_parent(labels=("ready-for-agent",))
        self.add_child(70, labels=("ready-for-agent",))
        path = self.add_run_worktree()
        confirmer = ScriptedConfirmer(True)

        self.assertEqual(self.reset(choose=ScriptedPicker(PARENT), confirm=confirmer), 0, self.stderr)

        self.assertIn(f"→ remove the run worktree of {RUN_BRANCH} at {path}", self.stdout)
        self.assertFalse(path.exists())
        self.assertEqual(self.branches(), set())

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
                self.assert_refused(PARENT, "#71 is merged", "merged work is never reset")

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

        self.assertIn(f"Rerun `macatia reset {PARENT}` to finish", self.stderr)
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

    def test_a_child_closed_after_an_interrupted_reset_stays_in_the_recap(self):
        self.add_parent()
        self.add_child(70)
        self.add_child(71)
        self.github.interrupt_after_comment_on = 71

        with self.assertRaises(RunInterrupted):
            self.reset(str(PARENT), "--yes")
        self.github.close_issue(70, "completed")

        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)

        self.assertEqual(
            reset_markers(self.github.comments(PARENT)),
            [{"children": [
                {"number": 70, "previous_state": "implemented"},
                {"number": 71, "previous_state": "implemented"},
            ]}],
        )

    def test_ctrl_c_while_inspecting_writes_nothing_and_says_so(self):
        self.add_parent()
        self.add_child(70)
        self.github.parent_error = {PARENT: KeyboardInterrupt()}
        before = self.snapshot()

        self.assertEqual(self.reset(str(PARENT), "--yes"), 130)

        self.assertIn("Nothing reset", self.stderr)
        self.assertEqual(self.snapshot(), before)

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
        self.github.label_edit_error = MacatiaError("command failed (gh issue edit 70): HTTP 502")

        self.assertEqual(self.reset(str(PARENT), "--yes"), 1)

        self.assertIn("HTTP 502", self.stderr)
        self.assertIn(f"Rerun `macatia reset {PARENT}` to finish", self.stderr)
        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)
        self.assert_reset_child(70, "implemented")
        self.assertEqual(len(reset_markers(self.github.comments(70))), 1)

    # The resettable run target picker.

    def add_resettable_and_refused_run_targets(self):
        """Parent #69 with two children to reset, standalone #80, parent #90 whose only trace
        is its run branch, and refused run targets around them."""
        self.add_parent()
        self.add_child(70)
        self.add_child(71, labels=("human-review",))
        self.add_child(72, labels=("ready-for-agent",))
        self.github.add_issue(80, labels=("implemented", "standalone"), comments=[self.evidence(80)])
        self.github.add_issue(90, labels=("ready-for-agent",))
        self.github.add_issue(91, parent=90, labels=("ready-for-agent",))
        git(self.repository.path, "push", "origin", f"{BRANCH}:refs/heads/macatia/issue-90")
        # Refused: merged, untrusted, closed, without the trace of a run.
        self.github.add_issue(81, labels=("implemented",), comments=[self.evidence(81, merged=True)])
        self.github.add_issue(82, labels=("implemented",), author="stranger")
        self.github.add_issue(83, labels=("implemented",), state="closed")
        self.github.add_issue(84, labels=("ready-for-agent",))
        # A run branch whose issue is closed, and a branch named only like a run branch.
        git(self.repository.path, "push", "origin", f"{BRANCH}:refs/heads/macatia/issue-83")
        git(self.repository.path, "push", "origin", f"{BRANCH}:refs/heads/macatia/issue-84x")

    def test_without_issue_the_picker_offers_exactly_the_resettable_run_targets(self):
        self.add_resettable_and_refused_run_targets()
        picker = ScriptedPicker(KeyboardInterrupt())

        self.reset(choose=picker)

        self.assertEqual(picker.offered, [[
            f"#{PARENT}  Issue {PARENT}  (parent · 2 children to reset)",
            "#80  Issue 80  (standalone)",
            "#90  Issue 90  (parent · 0 children to reset)",
        ]])

    def test_a_run_target_whose_only_trace_is_a_child_label_or_a_pull_request_is_offered(self):
        self.add_parent(labels=("ready-for-agent",))
        self.add_child(70)
        self.github.add_issue(80, labels=("ready-for-agent",))
        self.add_pull_request(head="macatia/issue-80", title="Implement Macatia issue #80")
        picker = ScriptedPicker(KeyboardInterrupt())

        self.reset(choose=picker)

        self.assertEqual(picker.offered, [[
            f"#{PARENT}  Issue {PARENT}  (parent · 1 children to reset)",
            "#80  Issue 80  (standalone)",
        ]])

    def test_a_run_target_whose_only_trace_is_an_unpushed_run_branch_is_offered(self):
        self.add_parent(labels=("ready-for-agent",))
        self.add_child(70, labels=("ready-for-agent",))
        self.add_run_branch(pushed=False)
        picker = ScriptedPicker(KeyboardInterrupt())

        self.reset(choose=picker)

        self.assertEqual(picker.offered, [[f"#{PARENT}  Issue {PARENT}  (parent · 0 children to reset)"]])

    def test_the_picked_run_target_resets_as_if_its_number_had_been_passed(self):
        self.add_resettable_and_refused_run_targets()
        confirmer = ScriptedConfirmer(True)

        code = self.reset(choose=ScriptedPicker(PARENT), confirm=confirmer)

        self.assertEqual(code, 0, self.stderr)
        self.assertEqual(confirmer.questions, [f"Reset #{PARENT}?"])
        self.assert_reset_child(70, "implemented")
        self.assert_reset_child(71, "human_review")
        self.assertEqual(self.github.labels(80) & self.lifecycle(), {"implemented"})

    def test_the_picker_preselects_the_checked_out_run_branch_issue_when_it_is_listed(self):
        self.add_resettable_and_refused_run_targets()
        for branch, preselected in (
            ("macatia/issue-80", 80),
            (RUN_BRANCH, PARENT),
            ("macatia/issue-8", None),
            ("macatia/issue-81", None),
            (BRANCH, None),
        ):
            with self.subTest(branch=branch):
                git(self.repository.path, "switch", "--force-create", branch, BRANCH)
                picker = ScriptedPicker(KeyboardInterrupt())
                self.reset(choose=picker)
                self.assertEqual(picker.preselected, [preselected])
        git(self.repository.path, "switch", "--detach")
        picker = ScriptedPicker(KeyboardInterrupt())
        self.reset(choose=picker)
        self.assertEqual(picker.preselected, [None])

    def test_without_a_resettable_run_target_reset_says_so_and_exits_0(self):
        self.github.add_issue(84, labels=("ready-for-agent",))
        picker = ScriptedPicker(84)
        before = self.snapshot()

        self.assertEqual(self.reset(choose=picker), 0, self.stderr)

        self.assertEqual(picker.offered, [])
        self.assertIn("No run target in lmpaul/buyr is resettable.", self.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_ctrl_c_in_the_picker_writes_nothing_and_exits_130(self):
        self.add_resettable_and_refused_run_targets()
        before = self.snapshot()

        self.assertEqual(self.reset(choose=ScriptedPicker(KeyboardInterrupt())), 130)

        self.assertIn("Cancelled. Nothing reset.", self.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("macatia/issue-90", git(self.repository.origin, "branch", "--list", "macatia/*"))

    def test_a_number_outside_the_resettable_run_targets_is_refused(self):
        self.add_resettable_and_refused_run_targets()
        before = self.snapshot()

        self.assertEqual(self.reset("--yes", choose=ScriptedPicker(84)), 1)

        self.assertIn("#84 is not one of the resettable run targets", self.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_remote_run_branches_are_found_with_a_single_remote_listing(self):
        self.add_resettable_and_refused_run_targets()
        listings = []
        run = subprocess.run

        def spy(arguments, *args, **kwargs):
            if "ls-remote" in arguments:
                listings.append(arguments)
            return run(arguments, *args, **kwargs)

        with mock.patch.object(subprocess, "run", spy):
            self.reset(choose=ScriptedPicker(KeyboardInterrupt()))

        self.assertEqual(len(listings), 1, listings)

    def test_a_rerun_after_a_failure_names_the_picked_run_target(self):
        self.add_parent()
        self.add_child(70)
        self.github.label_edit_error = MacatiaError("command failed (gh issue edit 70): HTTP 502")

        self.assertEqual(self.reset("--yes", choose=ScriptedPicker(PARENT)), 1)

        self.assertIn(f"Rerun `macatia reset {PARENT}` to finish", self.stderr)

    def test_without_issue_or_the_picker_reset_is_a_usage_error(self):
        self.add_parent()
        self.add_child(70)

        self.assertEqual(self.reset("--yes"), 2)

        self.assertIn("pass the issue to reset", self.stderr)

    def test_an_explicit_issue_never_opens_the_picker(self):
        self.add_resettable_and_refused_run_targets()
        picker = ScriptedPicker(80)

        self.assertEqual(self.reset("--yes", str(PARENT), choose=picker), 0, self.stderr)

        self.assertEqual(picker.offered, [])

    def test_the_command_line_passes_the_arrow_key_picker_only_at_a_terminal(self):
        for stdin_tty, stdout_tty, expected in (
            (True, True, cli.reset_target_with_arrows),
            (False, True, None),
            (True, False, None),
        ):
            with (
                self.subTest(stdin_tty=stdin_tty, stdout_tty=stdout_tty),
                mock.patch.object(sys.stdin, "isatty", return_value=stdin_tty),
                mock.patch.object(sys.stdout, "isatty", return_value=stdout_tty),
                mock.patch.object(reset, "run", return_value=0) as run,
                mock.patch.object(sys, "dont_write_bytecode", sys.dont_write_bytecode),
            ):
                self.assertEqual(cli.main(["reset"]), 0)
                self.assertIs(run.call_args.kwargs["choose_run_target"], expected)

    def test_the_arrow_key_picker_asks_which_run_target_to_reset(self):
        targets = [reset.ResettableRunTarget(12, "Add a cart", 3), reset.ResettableRunTarget(20, "Fix login", None)]
        with mock.patch.object(check, "arrow_select", return_value="#20  Fix login  (standalone)") as select:
            self.assertEqual(cli.reset_target_with_arrows(targets, 20), 20)
        select.assert_called_once_with(
            "Which run target should Macatia reset?",
            [("#12  Add a cart  (parent · 3 children to reset)", ""), ("#20  Fix login  (standalone)", "")],
            "#20  Fix login  (standalone)",
        )

    def test_usage_errors_exit_2(self):
        self.assertEqual(self.reset(), 2)
        self.assertEqual(self.reset("not-a-number"), 2)
        self.assertEqual(self.reset(str(PARENT), "--unknown"), 2)

    def test_the_command_line_lists_and_dispatches_reset(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["--help"]), 0)
        self.assertIn("reset [--yes] [--purge] [--config PATH] [ISSUE]", stdout.getvalue())
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as usage,
        ):
            cli.main(["reset"])
        self.assertEqual(usage.exception.code, 2)

    def test_the_configuration_path_is_honoured_and_named_in_the_rerun(self):
        (self.repository.path / "other.toml").write_text(
            (self.repository.path / "macatia.toml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.add_parent()
        self.add_child(70)
        self.github.label_edit_error = MacatiaError("HTTP 502")

        self.assertEqual(self.reset("--config", "other.toml", str(PARENT), "--yes"), 1)

        self.assertIn(f"macatia reset --config other.toml {PARENT}", self.stderr)

    # Purge.

    def macatia_comments(self):
        """A trusted comment carrying each Macatia marker, as the run, close and reset write them."""
        evidence = implementation_evidence(loop_core.STANDARD_RUN, sha("implementation"), sha("verified"))
        reviewed = implementation_evidence(loop_core.REVIEWED_RUN, sha("implementation"), sha("reviewed"))
        return [
            child_base_marker(sha("base")),
            evidence,
            reviewed,
            legacy_implementation_evidence(sha("base"), sha("legacy")),
            trusted(f"{loop_core.HANDOFF_MARKER}\nMacatia stopped: a human must review."),
            *(
                trusted(f"{marker}\nMacatia finished.")
                for mode in (loop_core.STANDARD_RUN, loop_core.REVIEWED_RUN)
                for marker in (mode.parent_completion_marker, mode.terminal_summary_marker)
            ),
            reset_marker(),
        ]

    def kept_comments(self):
        """Comments a purge leaves: untrusted ones, even carrying a Macatia marker, and trusted
        ones without one."""
        return [
            {"author": {"login": "someone"}, "body": child_base_marker(sha("base"))["body"]},
            trusted("Please also cover the empty case."),
        ]

    def test_a_purge_deletes_every_macatia_comment_on_the_parent_and_its_reset_children(self):
        kept = self.kept_comments()
        self.add_parent(labels=("work-started", "human-review"), comments=[*self.macatia_comments(), *kept])
        self.add_child(70, comments=[*kept, *self.macatia_comments()])
        self.add_child(71, labels=("human-review",), comments=self.macatia_comments())
        # Untouched: no lifecycle label, so its triage stays whatever it carries.
        self.add_child(72, labels=("needs-triage",), comments=[*kept, *self.macatia_comments()])

        self.assertEqual(self.reset(str(PARENT), "--purge", "--yes"), 0, self.stderr)

        for number in (PARENT, 70):
            self.assertEqual(self.github.comments(number), [comment["body"] for comment in kept])
        self.assertEqual(len(self.github.comments(72)), len(kept) + len(self.macatia_comments()))
        self.assertEqual(self.github.comments(71), [])
        self.assertEqual(self.github.labels(PARENT), {"ready-for-agent"})
        self.assertEqual(self.github.labels(70), {"ready-for-agent"})
        self.assertEqual(self.github.labels(71), {"ready-for-agent"})
        self.assertEqual(self.github.labels(72), {"needs-triage"})

    def test_a_purge_plan_counts_the_comments_it_deletes_on_each_issue(self):
        self.add_parent(comments=self.macatia_comments()[:2])
        self.add_child(70, comments=self.macatia_comments()[:3])
        self.add_child(71, labels=("human-review",), comments=[trusted("No marker.")])

        self.assertEqual(self.reset(str(PARENT), "--purge", "--yes"), 0, self.stderr)

        self.assertIn("#70 Issue 70: implemented → ready-for-agent, deleting 3 Macatia comments", self.stdout)
        self.assertIn("#71 Issue 71: human-review → ready-for-agent, deleting 0 Macatia comments", self.stdout)
        self.assertIn(f"then #{PARENT}: -work-started, +ready-for-agent, then deleting 2 Macatia comments", self.stdout)

    def test_a_purge_of_a_standalone_issue_deletes_its_macatia_comments_and_leaves_no_marker(self):
        kept = self.kept_comments()
        self.github.add_issue(80, labels=("implemented", "standalone"), comments=[*self.macatia_comments(), *kept])

        self.assertEqual(self.reset("80", "--purge", "--yes"), 0, self.stderr)

        self.assertEqual(self.github.labels(80), {"ready-for-agent", "standalone"})
        self.assertEqual(self.github.comments(80), [comment["body"] for comment in kept])
        self.assertIn(
            f"-implemented, +ready-for-agent, deleting {len(self.macatia_comments())} Macatia comments", self.stdout
        )

    def test_a_purge_leaves_the_pull_request_comments(self):
        self.add_parent()
        self.add_child(70)
        self.add_run_branch()
        self.add_pull_request()

        self.assertEqual(self.reset(str(PARENT), "--purge", "--yes"), 0, self.stderr)

        [comment] = self.github.pull_requests[0].comments
        self.assertIn(f"reset of #{PARENT} closed", comment)
        self.assertEqual(self.github.comments(70), [])
        self.assertEqual(self.github.comments(PARENT), [])

    def test_a_run_target_whose_only_trace_is_a_reset_marker_is_resettable_only_for_a_purge(self):
        self.add_parent(labels=("ready-for-agent",))
        self.add_child(70, labels=("ready-for-agent",), comments=[*self.macatia_comments()[:2], reset_marker()])
        self.assertEqual(self.reset(str(PARENT), "--yes"), 0, self.stderr)
        self.github.add_issue(80, labels=("ready-for-agent",), comments=[reset_marker()])

        self.assert_refused(PARENT, f"#{PARENT} is not resettable")
        self.assert_refused(80, "#80 is not resettable")

        self.assertEqual(self.reset(str(PARENT), "--purge", "--yes"), 0, self.stderr)
        self.assertEqual(self.reset("80", "--purge", "--yes"), 0, self.stderr)
        for number in (PARENT, 70, 80):
            self.assertEqual(self.github.comments(number), [])
            self.assertEqual(self.github.labels(number), {"ready-for-agent"})

    def test_without_issue_a_purge_also_offers_a_run_target_whose_only_trace_is_a_reset_marker(self):
        self.add_parent(labels=("ready-for-agent",), comments=[reset_marker()])
        self.add_child(70, labels=("ready-for-agent",), comments=[reset_marker()])
        self.github.add_issue(80, labels=("ready-for-agent",), comments=[reset_marker()])
        self.github.add_issue(81, labels=("ready-for-agent",), comments=[trusted("No marker.")])
        plain, purge = ScriptedPicker(KeyboardInterrupt()), ScriptedPicker(KeyboardInterrupt())

        self.assertEqual(self.reset(choose=plain), 0, self.stderr)
        self.assertEqual(self.reset("--purge", choose=purge), 130)

        self.assertEqual(plain.offered, [])
        self.assertEqual(purge.offered, [[
            f"#{PARENT}  Issue {PARENT}  (parent · 1 children to reset)",
            "#80  Issue 80  (standalone)",
        ]])

    def test_a_rerun_after_an_interrupted_purge_finishes_it(self):
        self.add_parent(comments=self.macatia_comments())
        self.add_child(70, comments=self.macatia_comments())
        self.add_child(71, labels=("human-review",), comments=self.macatia_comments())
        deleted = []

        def interrupt(number):
            deleted.append(number)
            if deleted.count(71) == 2:
                raise KeyboardInterrupt

        self.github.after_comment_delete = interrupt

        self.assertEqual(self.reset(str(PARENT), "--purge", "--yes"), 130)

        self.assertIn(f"Rerun `macatia reset --purge {PARENT}` to finish", self.stderr)
        self.assertEqual(self.github.comments(70), [])
        self.assertEqual(len(self.github.comments(71)), len(self.macatia_comments()) - 2)
        self.assertEqual(self.github.labels(71), {"human-review"})
        self.github.after_comment_delete = None

        self.assertEqual(self.reset(str(PARENT), "--purge", "--yes"), 0, self.stderr)

        for number in (PARENT, 70, 71):
            self.assertEqual(self.github.comments(number), [])
            self.assertEqual(self.github.labels(number), {"ready-for-agent"})

    def test_a_rerun_after_a_purge_interrupted_before_the_parent_comments_finishes_it(self):
        self.add_parent(comments=self.macatia_comments())
        self.add_child(70)

        def interrupt(number, add, remove):
            if number == PARENT:
                raise KeyboardInterrupt

        self.github.after_label_edit = interrupt
        self.assertEqual(self.reset(str(PARENT), "--purge", "--yes"), 130)
        self.github.after_label_edit = None
        self.assertEqual(self.github.labels(PARENT), {"ready-for-agent"})

        self.assertEqual(self.reset(str(PARENT), "--purge", "--yes"), 0, self.stderr)

        self.assertEqual(self.github.comments(PARENT), [])

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
