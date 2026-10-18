"""Opt-in live suite: the only proof of compatibility with the real gh, codex and claude.

Skipped unless RALPH_LIVE=1. It needs authenticated CLIs and spends tokens;
see "Live suite" in the README for when and how to run it.
"""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from live_support import (
    FIXTURE_PROMPT,
    KIT_ROOT,
    cli_version,
    record_engine_fixture,
    run_live_session,
    scratch_repository,
)
from ralph_kit.github import GitHub
from ralph_kit.loop_core import (
    check_runs_for_head,
    classify_ci_failure,
    issue_details,
    labels,
)


LIVE = os.environ.get("RALPH_LIVE") == "1"
REFRESH = os.environ.get("RALPH_LIVE_REFRESH") == "1"
SKIP_REASON = (
    "live suite against real gh, codex and claude; set RALPH_LIVE=1 to run it "
    "(spends tokens, see 'Live suite' in the README)"
)
# A parent with sub-issues and blockers that stay in place after they close.
REPOSITORY = os.environ.get("RALPH_LIVE_REPOSITORY", "lmpaul/ralph-kit")
PARENT = int(os.environ.get("RALPH_LIVE_PARENT", "5"))
CLAUDE_MODEL = os.environ.get("RALPH_LIVE_CLAUDE_MODEL", "haiku")
CODEX_MODEL = os.environ.get("RALPH_LIVE_CODEX_MODEL", "gpt-5.6-luna")
REASONING_EFFORT = "low"


@unittest.skipUnless(LIVE, SKIP_REASON)
class LiveGitHubTests(unittest.TestCase):
    """Read-only `gh` calls of each shape a run parses, through Ralph's GitHub adapter."""

    @classmethod
    def setUpClass(cls):
        cls.github = GitHub(KIT_ROOT, REPOSITORY)
        # The run's parsing helpers only reach GitHub through `state.github`.
        cls.state = SimpleNamespace(github=cls.github)

    def assert_issue_summary(self, issue):
        self.assertIsInstance(issue["number"], int)
        self.assertIn(str(issue["state"]).upper(), {"OPEN", "CLOSED"})
        self.assertIsInstance(issue["labels"], list)
        self.assertTrue(all(isinstance(name, str) for name in labels(issue)))

    def test_sub_issues_list_each_child_with_its_number_state_and_labels(self):
        children = self.github.sub_issues(PARENT)

        self.assertTrue(children, f"#{PARENT} has no sub-issues in {REPOSITORY}")
        for child in children:
            self.assert_issue_summary(child)

    def test_blockers_list_each_blocking_issue_with_its_number_state_and_labels(self):
        blockers = [
            blocker
            for child in self.github.sub_issues(PARENT)
            for blocker in self.github.blockers(child["number"])
        ]

        self.assertTrue(blockers, f"no sub-issue of #{PARENT} has a blocker in {REPOSITORY}")
        for blocker in blockers:
            self.assert_issue_summary(blocker)

    def test_issue_details_carry_the_trusted_author_labels_and_comment_authors(self):
        child = self.github.sub_issues(PARENT)[0]["number"]

        details = issue_details(self.state, child)

        self.assertEqual(details["number"], child)
        self.assertIsInstance(details["title"], str)
        self.assertIsInstance(details["body"], str)
        self.assertIsInstance(details["author"]["login"], str)
        self.assertTrue(all(isinstance(name, str) for name in labels(details)))
        for comment in details["comments"]:
            self.assertIsInstance(comment["author"]["login"], str)
            self.assertIsInstance(comment["body"], str)

    def test_check_runs_for_the_default_branch_head_pass_ralph_ci_classification(self):
        head = subprocess.run(
            ["gh", "api", f"repos/{REPOSITORY}/commits/HEAD", "--jq", ".sha"],
            check=True, text=True, capture_output=True,
        ).stdout.strip()

        runs = check_runs_for_head(self.state, head)

        self.assertTrue(runs, f"{REPOSITORY}@{head} has no check runs")
        for run in runs:
            self.assertEqual(run["head_sha"], head)
            self.assertIsInstance(run["id"], int)
            self.assertIsInstance(run["name"], str)
            self.assertIn("conclusion", run)
        passed = {run["name"]: "required" for run in runs if run["conclusion"] == "success"}
        self.assertTrue(passed, f"{REPOSITORY}@{head} has no successful check run")
        self.assertIsNone(classify_ci_failure(head, runs, passed))


@unittest.skipUnless(LIVE, SKIP_REASON)
class LiveEngineTests(unittest.TestCase):
    """One real session per engine, with Ralph's command, output schema and result parsing.

    With RALPH_LIVE_REFRESH=1, each session also replaces that engine's recorded fixture.
    """

    expected = {"outcome": "no_changes", "summary": "Fixture session: nothing to do.", "findings": []}

    def run_engine_session(self, engine, model):
        with tempfile.TemporaryDirectory(prefix="ralph-live-repository-") as directory:
            repository = scratch_repository(directory)
            session = run_live_session(engine, model, REASONING_EFFORT, repository, prompt=FIXTURE_PROMPT)
            status = subprocess.run(
                ["git", "status", "--porcelain"], cwd=repository, check=True, text=True, capture_output=True
            ).stdout
        self.assertEqual(session.result, self.expected)
        self.assertEqual(status, "", f"{engine} changed the scratch repository")
        if REFRESH:
            record_engine_fixture(
                engine, session, model=model, reasoning_effort=REASONING_EFFORT, version=cli_version(engine)
            )

    def test_claude_code_session_returns_a_result_ralph_accepts(self):
        self.run_engine_session("claude", CLAUDE_MODEL)

    def test_codex_session_returns_a_result_ralph_accepts(self):
        self.run_engine_session("codex", CODEX_MODEL)


if __name__ == "__main__":
    unittest.main()
