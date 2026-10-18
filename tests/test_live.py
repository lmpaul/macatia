"""Opt-in live suite: the only proof of compatibility with the real gh, codex and claude.

Skipped unless RALPH_LIVE=1. It needs authenticated CLIs and spends tokens;
see "Live suite" in the README for when and how to run it.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from live_support import (
    FIXTURE_RESULT,
    KIT_ROOT,
    cli_version,
    record_engine_fixture,
    run_live_session,
    scratch_repository,
)
from ralph_kit.github import GitHub
from ralph_kit.loop_core import (
    actions_job_id,
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
# A parent issue of this repository whose children and blockers stay in place after they close.
PARENT = int(os.environ.get("RALPH_LIVE_PARENT", "5"))
CLAUDE_MODEL = os.environ.get("RALPH_LIVE_CLAUDE_MODEL", "haiku")
CODEX_MODEL = os.environ.get("RALPH_LIVE_CODEX_MODEL", "gpt-5.6-luna")
REASONING_EFFORT = "low"


@unittest.skipUnless(LIVE, SKIP_REASON)
class LiveGitHubTests(unittest.TestCase):
    """Read-only `gh` calls of each shape a run parses, through Ralph's GitHub adapter."""

    @classmethod
    def setUpClass(cls):
        # A run resolves its repository the same way, from the checkout it runs in.
        cls.repository = GitHub(KIT_ROOT, "unresolved").repository_name()
        cls.github = GitHub(KIT_ROOT, cls.repository)
        # The run's parsing helpers only reach GitHub through `state.github`.
        cls.state = SimpleNamespace(github=cls.github)

    def assert_issue_summary(self, issue):
        self.assertIsInstance(issue["number"], int)
        self.assertIn(str(issue["state"]).upper(), {"OPEN", "CLOSED"})
        self.assertIsInstance(issue["labels"], list)
        self.assertTrue(all(isinstance(name, str) for name in labels(issue)))

    def test_repository_name_is_the_owner_and_name_of_the_checkout(self):
        self.assertRegex(self.repository, r"^[\w.-]+/[\w.-]+$")

    def test_children_list_each_child_with_its_number_state_and_labels(self):
        children = self.github.sub_issues(PARENT)

        self.assertTrue(children, f"parent issue #{PARENT} has no children in {self.repository}")
        for child in children:
            self.assert_issue_summary(child)

    def test_blockers_list_each_blocking_issue_with_its_number_state_and_labels(self):
        blockers = [
            blocker
            for child in self.github.sub_issues(PARENT)
            for blocker in self.github.blockers(child["number"])
        ]

        self.assertTrue(blockers, f"no child of parent issue #{PARENT} has a blocker in {self.repository}")
        for blocker in blockers:
            self.assert_issue_summary(blocker)

    def test_issue_details_carry_the_trusted_author_labels_and_comment_authors(self):
        child = self.github.sub_issues(PARENT)[0]["number"]

        details = issue_details(self.state, child)

        self.assertEqual(details["number"], child)
        self.assertTrue(details["comments"], f"child #{child} has no comment to check")
        self.assertIsInstance(details["title"], str)
        self.assertIsInstance(details["body"], str)
        self.assertIsInstance(details["author"]["login"], str)
        self.assertTrue(all(isinstance(name, str) for name in labels(details)))
        for comment in details["comments"]:
            self.assertIsInstance(comment["author"]["login"], str)
            self.assertIsInstance(comment["body"], str)

    def gh_json(self, *arguments):
        """A read-only `gh` query outside Ralph's adapter, to find something for it to read."""
        return json.loads(subprocess.run(
            ["gh", *arguments], cwd=KIT_ROOT, check=True, text=True, capture_output=True
        ).stdout)

    def default_branch_head(self):
        return self.gh_json("api", f"repos/{self.repository}/commits/HEAD")["sha"]

    def test_open_pull_requests_list_each_pull_request_with_its_number_draft_state_and_url(self):
        open_pulls = self.gh_json(
            "pr", "list", "--state", "open", "--limit", "1", "--json", "headRefName,baseRefName"
        )
        if not open_pulls:
            self.skipTest(f"{self.repository} has no open pull request")
        (pull,) = open_pulls

        pulls = self.github.open_pull_requests(pull["headRefName"], pull["baseRefName"])

        self.assertIsInstance(pulls, list)
        self.assertTrue(pulls)
        for listed in pulls:
            self.assertIsInstance(listed["number"], int)
            self.assertIsInstance(listed["isDraft"], bool)
            self.assertIsInstance(listed["url"], str)

    def test_actions_job_its_log_and_its_check_run_match_the_check_they_come_from(self):
        head = self.default_branch_head()
        check = next(
            (run for run in check_runs_for_head(self.state, head)
             if actions_job_id(str(run.get("details_url") or ""))),
            None,
        )
        self.assertIsNotNone(check, f"{self.repository}@{head} has no check run from an Actions job")
        job_id = actions_job_id(check["details_url"])

        job = self.github.actions_job(job_id)
        log = self.github.job_log(job_id)
        check_run = self.github.check_run(check["id"])

        self.assertEqual(job.returncode, 0, job.stderr)
        job_metadata = json.loads(job.stdout)
        self.assertEqual(job_metadata["head_sha"], head)
        self.assertTrue(job_metadata["check_run_url"].endswith(f"/check-runs/{check['id']}"))
        self.assertEqual(log.returncode, 0, log.stderr)
        self.assertTrue(log.stdout.strip(), f"job {job_id} has an empty log")
        self.assertEqual(check_run.returncode, 0, check_run.stderr)
        self.assertIsInstance(json.loads(check_run.stdout)["output"], dict)

    def test_check_runs_for_the_default_branch_head_pass_ralph_ci_classification(self):
        head = self.default_branch_head()

        runs = check_runs_for_head(self.state, head)

        self.assertTrue(runs, f"{self.repository}@{head} has no check runs")
        for run in runs:
            self.assertEqual(run["head_sha"], head)
            self.assertIsInstance(run["id"], int)
            self.assertIsInstance(run["name"], str)
            self.assertIn("conclusion", run)
        passed = {run["name"]: "required" for run in runs if run["conclusion"] == "success"}
        self.assertTrue(passed, f"{self.repository}@{head} has no successful check run")
        self.assertIsNone(classify_ci_failure(head, runs, passed))


@unittest.skipUnless(LIVE, SKIP_REASON)
class LiveEngineTests(unittest.TestCase):
    """One real session per engine, with Ralph's command, output schema and result parsing.

    With RALPH_LIVE_REFRESH=1, each session also replaces that engine's recorded fixture.
    """

    def run_engine_session(self, engine, model):
        with tempfile.TemporaryDirectory(prefix="ralph-live-repository-") as directory:
            repository = scratch_repository(directory)
            session = run_live_session(engine, model, REASONING_EFFORT, repository)
            status = subprocess.run(
                ["git", "status", "--porcelain"], cwd=repository, check=True, text=True, capture_output=True
            ).stdout
        self.assertEqual(session.result, FIXTURE_RESULT)
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
