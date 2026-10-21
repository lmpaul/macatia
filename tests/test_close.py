"""`ralph-kit close` in memory: the fake GitHub, a scripted confirmer, no Git checkout."""

import contextlib
import hashlib
import io
import pathlib
import shutil
import sys
import tempfile
import unittest


TESTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "src"))
sys.path.insert(0, str(TESTS))

from ralph_kit import cli, close  # noqa: E402
from ralph_kit.commands import RalphError  # noqa: E402
from ralph_kit.loop_core import REVIEWED_RUN, STANDARD_RUN  # noqa: E402
from run_support import (  # noqa: E402
    FIXTURES,
    FakeGitHub,
    implementation_evidence,
    legacy_implementation_evidence,
)


PARENT = 69
NO_EVIDENCE = "implemented without evidence: merge not verified"


def sha(name):
    """A distinct 40-hex commit SHA for a name."""
    return hashlib.sha1(name.encode()).hexdigest()


class ScriptedConfirmer:
    """Answers each Yes/No question in turn; an answer can be an exception to raise."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.questions = []

    def __call__(self, question):
        self.questions.append(question)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class CloseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        # A plain directory, not a Git checkout: closing needs only the configuration.
        self.root = pathlib.Path(directory.name).resolve()
        shutil.copyfile(FIXTURES / "ralph.toml", self.root / "ralph.toml")
        self.github = FakeGitHub()

    def add_parent(self, *, labels=("human-review",), **options):
        return self.github.add_issue(PARENT, labels=labels, **options)

    def add_child(self, number, *, labels=("implemented",), comments=None, **options):
        """A child; by default with standard run evidence whose verified head is merged."""
        if comments is None:
            comments = [self.evidence(number)]
        return self.github.add_issue(number, parent=PARENT, labels=labels, comments=comments, **options)

    def evidence(self, number, *, mode=STANDARD_RUN, merged=True, version="v1"):
        verified_head = sha(f"verified-{number}-{version}")
        self.github.on_branch[verified_head] = merged
        return implementation_evidence(mode, sha(f"implementation-{number}-{version}"), verified_head)

    def close(self, *arguments, confirm=None):
        """Run `ralph-kit close`; confirm=None stands for no terminal."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = close.run(
                    list(arguments),
                    github=lambda _root, _repository: self.github,
                    confirm=confirm,
                    repository_root=self.root,
                )
            except SystemExit as error:  # argparse usage errors.
                code = error.code
        self.stdout, self.stderr = stdout.getvalue(), stderr.getvalue()
        return code

    def state(self, number):
        issue = self.github.issues[number]
        return issue["state"], issue.get("stateReason")

    def assert_nothing_written(self, *numbers):
        for number in numbers:
            self.assertEqual(self.state(number), ("OPEN", None))
        self.assertEqual(self.github.comments(PARENT), [])

    def test_closes_open_children_then_comments_on_and_closes_the_parent(self):
        self.add_parent()
        self.add_child(70)
        self.add_child(71)
        closings = []
        self.github.after_close = lambda number, reason: closings.append(number)

        code = self.close(str(PARENT), "--yes")

        self.assertEqual(code, 0, self.stderr)
        self.assertEqual(closings, [70, 71, PARENT])
        for number in (70, 71, PARENT):
            self.assertEqual(self.state(number), ("CLOSED", "COMPLETED"))
        [comment] = self.github.comments(PARENT)
        self.assertIn("#70", comment)
        self.assertIn("#71", comment)
        self.assertEqual(self.github.labels(70), {"implemented"})
        self.assertEqual(self.github.labels(PARENT), {"human-review"})
        self.assertIn(f"Closed #{PARENT}", self.stdout)

    def test_the_parent_comment_comes_before_the_parent_closes(self):
        self.add_parent()
        self.add_child(70)
        parent_state_at_comment = []
        self.github.after_comment = lambda number, _body: parent_state_at_comment.append(
            self.state(number)
        )

        self.assertEqual(self.close(str(PARENT), "--yes"), 0, self.stderr)

        self.assertEqual(parent_state_at_comment, [("OPEN", None)])

    def test_closed_children_are_ignored_whatever_their_labels(self):
        self.add_parent()
        self.add_child(70, state="closed", labels=("needs-triage", "agent-working"))
        self.add_child(71)

        self.assertEqual(self.close(str(PARENT), "--yes"), 0, self.stderr)

        self.assertEqual(self.state(70), ("CLOSED", None))
        self.assertEqual(self.state(71), ("CLOSED", "COMPLETED"))
        [comment] = self.github.comments(PARENT)
        self.assertIn("#71", comment)
        self.assertNotIn("#70", comment)

    def test_a_closed_parent_is_already_closed(self):
        self.add_parent(state="closed")
        self.add_child(70)

        self.assertEqual(self.close(str(PARENT), "--yes"), 0)

        self.assertIn("already closed", self.stdout)
        self.assertEqual(self.state(70), ("OPEN", None))
        self.assertEqual(self.github.comments(PARENT), [])

    def test_a_nonexistent_issue_is_refused(self):
        self.assertEqual(self.close("404", "--yes"), 1)

        self.assertIn("#404", self.stderr)

    def test_an_issue_without_sub_issues_is_refused(self):
        self.add_parent()

        self.assertEqual(self.close(str(PARENT), "--yes"), 1)

        self.assertIn("no sub-issues", self.stderr)
        self.assert_nothing_written(PARENT)

    def test_a_refusal_lists_every_failed_check_and_writes_nothing(self):
        self.add_parent(labels=("ready-for-agent",), author="stranger")
        self.add_child(70, author="intruder")
        self.add_child(71, labels=("ready-for-agent",))
        self.add_child(72, labels=("implemented", "human-review"))
        self.add_child(73)

        code = self.close(str(PARENT), "--yes", confirm=ScriptedConfirmer())

        self.assertEqual(code, 1)
        for failure in (
            f"#{PARENT} is not labelled human-review",
            f"#{PARENT} is authored by @stranger",
            "#70 is authored by @intruder",
            "#71 is not implemented",
            "#72 is not implemented",
        ):
            self.assertIn(failure, self.stderr)
        self.assertNotIn("#73", self.stderr)
        self.assert_nothing_written(PARENT, 70, 71, 72, 73)

    def test_an_open_child_needs_implemented_as_its_only_lifecycle_label(self):
        self.add_parent()
        self.add_child(70, labels=("implemented", "agent-working"))
        self.add_child(71, labels=())
        # Triage labels other than lifecycle ones do not count.
        self.add_child(72, labels=("implemented", "needs-info"))

        self.assertEqual(self.close(str(PARENT), "--yes"), 1)

        self.assertIn("#70 is not implemented", self.stderr)
        self.assertIn("#71 is not implemented", self.stderr)
        self.assertNotIn("#72", self.stderr)
        self.assert_nothing_written(PARENT, 70, 71, 72)

    def test_the_confirmation_lists_what_will_be_closed(self):
        self.add_parent()
        self.add_child(70)
        self.add_child(71, state="closed")
        confirmer = ScriptedConfirmer(True)

        self.assertEqual(self.close(str(PARENT), confirm=confirmer), 0, self.stderr)

        self.assertEqual(len(confirmer.questions), 1)
        self.assertIn("#70 Issue 70", self.stdout)
        self.assertNotIn("#71", self.stdout)
        self.assertIn(f"#{PARENT} Issue {PARENT}", self.stdout)
        self.assertEqual(self.state(PARENT), ("CLOSED", "COMPLETED"))

    def test_declining_the_confirmation_writes_nothing(self):
        self.add_parent()
        self.add_child(70)

        self.assertEqual(self.close(str(PARENT), confirm=ScriptedConfirmer(False)), 1)

        self.assertIn("Nothing closed", self.stderr)
        self.assert_nothing_written(PARENT, 70)

    def test_yes_skips_the_confirmation(self):
        self.add_parent()
        self.add_child(70)
        confirmer = ScriptedConfirmer()

        self.assertEqual(self.close(str(PARENT), "--yes", confirm=confirmer), 0, self.stderr)

        self.assertEqual(confirmer.questions, [])

    def test_without_a_terminal_or_yes_the_command_refuses(self):
        self.add_parent()
        self.add_child(70)

        self.assertEqual(self.close(str(PARENT)), 1)

        self.assertIn("--yes", self.stderr)
        self.assert_nothing_written(PARENT, 70)

    def test_ctrl_c_at_the_confirmation_exits_130_and_writes_nothing(self):
        self.add_parent()
        self.add_child(70)

        code = self.close(str(PARENT), confirm=ScriptedConfirmer(KeyboardInterrupt()))

        self.assertEqual(code, 130)
        self.assert_nothing_written(PARENT, 70)

    def test_a_github_error_while_closing_exits_1_and_a_rerun_finishes_the_closure(self):
        self.add_parent()
        self.add_child(70)
        self.add_child(71)
        self.github.close_error = {71: RalphError("command failed (gh issue close 71): HTTP 502")}

        self.assertEqual(self.close(str(PARENT), "--yes"), 1)

        self.assertIn("HTTP 502", self.stderr)
        self.assertEqual(self.state(70), ("CLOSED", "COMPLETED"))
        self.assertEqual(self.state(71), ("OPEN", None))
        self.assertEqual(self.state(PARENT), ("OPEN", None))
        self.assertEqual(self.github.comments(PARENT), [])

        self.assertEqual(self.close(str(PARENT), "--yes"), 0, self.stderr)

        self.assertEqual(self.state(71), ("CLOSED", "COMPLETED"))
        self.assertEqual(self.state(PARENT), ("CLOSED", "COMPLETED"))
        [comment] = self.github.comments(PARENT)
        self.assertIn("#71", comment)
        self.assertNotIn("#70", comment)

    def test_a_rerun_after_the_parent_failed_to_close_does_not_comment_again(self):
        self.add_parent()
        self.add_child(70)
        self.github.close_error = {PARENT: RalphError("command failed (gh issue close 69): HTTP 502")}

        self.assertEqual(self.close(str(PARENT), "--yes"), 1)
        self.assertEqual(self.close(str(PARENT), "--yes"), 0, self.stderr)

        self.assertEqual(self.state(PARENT), ("CLOSED", "COMPLETED"))
        [comment] = self.github.comments(PARENT)
        self.assertIn("#70", comment)

    def test_a_rerun_after_every_child_closed_still_closes_the_parent(self):
        self.add_parent()
        self.add_child(70, state="closed")

        self.assertEqual(self.close(str(PARENT), "--yes"), 0, self.stderr)

        self.assertEqual(self.state(PARENT), ("CLOSED", "COMPLETED"))
        self.assertEqual(len(self.github.comments(PARENT)), 1)

    def test_a_configuration_path_is_read_relative_to_the_root(self):
        configuration = (self.root / "ralph.toml").read_text(encoding="utf-8")
        (self.root / "other.toml").write_text(
            configuration.replace('human_review = "human-review"', 'human_review = "handoff"'),
            encoding="utf-8",
        )
        self.add_parent(labels=("handoff",))
        self.add_child(70)

        self.assertEqual(self.close(str(PARENT), "--config", "other.toml", "--yes"), 0, self.stderr)

        self.assertEqual(self.state(PARENT), ("CLOSED", "COMPLETED"))

    def test_a_missing_configuration_is_refused(self):
        self.assertEqual(self.close(str(PARENT), "--config", "missing.toml", "--yes"), 1)

        self.assertIn("missing.toml", self.stderr)

    def test_a_child_whose_verified_head_is_not_on_the_base_branch_is_refused(self):
        self.add_parent()
        self.add_child(70, comments=[self.evidence(70, merged=False)])
        self.add_child(71, labels=("ready-for-agent",))
        self.add_child(72)

        self.assertEqual(self.close(str(PARENT), "--yes"), 1)

        self.assertIn(f"#70 is not merged: its verified head {sha('verified-70-v1')} is not on main", self.stderr)
        self.assertIn("#71 is not implemented", self.stderr)
        self.assertNotIn("#72", self.stderr)
        self.assert_nothing_written(PARENT, 70, 71, 72)

    def test_the_merge_check_compares_the_base_branch_with_the_verified_head(self):
        self.add_parent()
        self.add_child(70)

        self.assertEqual(self.close(str(PARENT), "--yes"), 0, self.stderr)

        self.assertEqual(self.github.compared, [("main", sha("verified-70-v1"))])

    def test_evidence_from_either_run_and_the_legacy_format_counts(self):
        self.add_parent()
        self.add_child(70, comments=[self.evidence(70, mode=STANDARD_RUN)])
        self.add_child(71, comments=[self.evidence(71, mode=REVIEWED_RUN)])
        legacy_head = sha("legacy-72")
        self.github.on_branch[legacy_head] = True
        self.add_child(72, comments=[legacy_implementation_evidence(sha("base-72"), legacy_head)])
        confirmer = ScriptedConfirmer()

        self.assertEqual(self.close(str(PARENT), "--yes", confirm=confirmer), 0, self.stderr)

        self.assertEqual(confirmer.questions, [])
        self.assertNotIn(NO_EVIDENCE, self.stdout)
        self.assertIn(legacy_head, {head for _branch, head in self.github.compared})

    def test_the_latest_trusted_evidence_wins(self):
        self.add_parent()
        self.add_child(70, comments=[
            self.evidence(70, version="old", merged=True),
            self.evidence(70, mode=REVIEWED_RUN, version="new", merged=False),
        ])
        self.add_child(71, comments=[
            self.evidence(71, version="old", merged=False),
            self.evidence(71, version="new", merged=True),
        ])

        self.assertEqual(self.close(str(PARENT), "--yes"), 1)

        self.assertIn(f"#70 is not merged: its verified head {sha('verified-70-new')}", self.stderr)
        self.assertNotIn("#71", self.stderr)

    def test_untrusted_evidence_is_ignored(self):
        self.add_parent()
        untrusted = {**self.evidence(70, version="untrusted", merged=False), "author": {"login": "intruder"}}
        self.add_child(70, comments=[self.evidence(70), untrusted])

        self.assertEqual(self.close(str(PARENT), "--yes"), 0, self.stderr)

        self.assertEqual(self.github.compared, [("main", sha("verified-70-v1"))])

    def test_an_implemented_child_without_trusted_evidence_is_listed_apart(self):
        self.add_parent()
        self.add_child(70)
        untrusted = {**self.evidence(71), "author": {"login": "intruder"}}
        self.add_child(71, comments=[untrusted])
        confirmer = ScriptedConfirmer(True)

        self.assertEqual(self.close(str(PARENT), confirm=confirmer), 0, self.stderr)

        listing = self.stdout.split(NO_EVIDENCE, 1)
        self.assertEqual(len(listing), 2, self.stdout)
        self.assertNotIn("#71", listing[0])
        self.assertIn("#71 Issue 71", listing[1])
        self.assertEqual(self.github.compared, [("main", sha("verified-70-v1"))])
        self.assertEqual(self.state(71), ("CLOSED", "COMPLETED"))

    def test_yes_does_not_skip_the_confirmation_of_a_child_without_evidence(self):
        self.add_parent()
        self.add_child(70, comments=[])
        confirmer = ScriptedConfirmer(False)

        self.assertEqual(self.close(str(PARENT), "--yes", confirm=confirmer), 1)

        self.assertEqual(len(confirmer.questions), 1)
        self.assert_nothing_written(PARENT, 70)

    def test_a_child_without_evidence_without_a_terminal_is_refused_even_with_yes(self):
        self.add_parent()
        self.add_child(70, comments=[])

        self.assertEqual(self.close(str(PARENT), "--yes"), 1)

        self.assertIn("#70", self.stderr)
        self.assertIn("terminal", self.stderr)
        self.assert_nothing_written(PARENT, 70)

    def test_the_parent_comment_lists_children_without_evidence_apart(self):
        self.add_parent()
        self.add_child(70)
        self.add_child(71, comments=[])

        self.assertEqual(self.close(str(PARENT), "--yes", confirm=ScriptedConfirmer(True)), 0, self.stderr)

        [comment] = self.github.comments(PARENT)
        proven, unproven = comment.split("confirmed by a human without implementation evidence", 1)
        self.assertIn("#70", proven)
        self.assertNotIn("#71", proven)
        self.assertIn("#71", unproven)
        self.assertNotIn("#70", unproven)

    def test_the_command_line_lists_and_dispatches_close(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["--help"]), 0)
        self.assertIn("close [--yes] [--config PATH] PARENT", stdout.getvalue())
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as usage,
        ):
            cli.main(["close"])
        self.assertEqual(usage.exception.code, 2)

    def test_usage_errors_exit_2(self):
        self.assertEqual(self.close(), 2)
        self.assertEqual(self.close("not-a-number"), 2)
        self.assertEqual(self.close(str(PARENT), "--unknown"), 2)


if __name__ == "__main__":
    unittest.main()
