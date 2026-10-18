import json
import unittest

from harness import (
    RalphProcessTestCase,
    run,
)


class ReviewLoopTests(RalphProcessTestCase):
    RUN_OPTIONS = ("--review",)

    def assert_only_safe_terminal_summary_was_published(self):
        github_calls = self._json_lines(self.github_log)
        self.assertFalse(
            any(
                call[:2] in (["pr", "create"], ["issue", "edit"])
                for call in github_calls
            )
        )
        issue_comments = [
            call for call in github_calls if call[:2] == ["issue", "comment"]
        ]
        self.assertEqual(len(issue_comments), 1)
        self.assertEqual(issue_comments[0][:3], ["issue", "comment", "191"])
        self.assertIn("<!-- ralph-terminal-summary -->", issue_comments[0][-1])
        self.assertIn("final outcome: stopped safely.", issue_comments[0][-1])

    def test_arguments_must_be_positive_integers(self):
        for arguments in (("0",), ("abc",), ("191", "0"), ("191", "abc")):
            with self.subTest(arguments=arguments):
                completed = self._run_loop(*arguments)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("positive integer", completed.stderr)
        self.assert_codex_did_not_start()

    def test_claude_cli_failure_reports_exit_code_and_stderr(self):
        self._configure_engines(implementation="claude")
        result = self._run_loop("191", environment=self.environment | {"RALPH_TEST_CLAUDE_CLI_ERROR": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Claude Code exited with code 1: Error: --json-schema is not a valid JSON Schema",
            result.stderr,
        )
        self.assertFalse(self.codex_log.exists())
        self.assert_only_safe_terminal_summary_was_published()

    def test_unknown_engine_is_rejected_before_agents_start(self):
        self._configure_engines(implementation="unknown")
        result = self._run_loop("191")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("engine must be codex or claude", result.stderr)
        self.assert_codex_did_not_start()

    def test_unsupported_claude_effort_is_rejected_before_agents_start(self):
        self._configure_engines(implementation="claude")
        path = self.repository / "ralph.toml"
        path.write_text(path.read_text().replace('reasoning_effort = "medium"', 'reasoning_effort = "ultra"'))
        result = self._run_loop("191")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reasoning_effort must be one of", result.stderr)
        self.assert_codex_did_not_start()

    def test_duplicate_toml_key_is_rejected_before_codex_starts(self):
        config_path = self.repository / "ralph.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8") + 'ci_checks = true\n',
            encoding="utf-8",
        )

        completed = self._run_loop("191")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("invalid TOML", completed.stderr)
        self.assert_codex_did_not_start()

    def test_repository_trust_remote_and_labels_come_from_toml(self):
        config_path = self.repository / "ralph.toml"
        contents = config_path.read_text(encoding="utf-8")
        contents = contents.replace(
            'repository = "lmpaul/buyr"', 'repository = "acme/widget"'
        )
        contents = contents.replace('remote = "origin"', 'remote = "upstream"')
        contents = contents.replace(
            'trusted_authors = ["lmpaul"]', 'trusted_authors = ["maintainer"]'
        )
        for original, configured in {
            "needs-triage": "r-needs-triage",
            "needs-info": "r-needs-info",
            "ready-for-agent": "r-ready-for-agent",
            "ready-for-human": "r-ready-for-human",
            "wontfix": "r-wontfix",
            "human-review": "r-human-review",
            "implemented": "r-implemented",
        }.items():
            contents = contents.replace(
                f'= "{original}"\n', f'= "{configured}"\n'
            )
        config_path.write_text(contents, encoding="utf-8")
        run(
            ["git", "remote", "add", "upstream", str(self.remote)],
            cwd=self.repository,
        )
        state = json.loads(self.github_state.read_text(encoding="utf-8"))
        state["parent"]["author"] = {"login": "maintainer"}
        state["parent"]["labels"] = [{"name": "r-ready-for-agent"}]
        for child in state["children"]:
            child["author"] = {"login": "maintainer"}
            child["labels"] = [
                {
                    "name": (
                        "r-ready-for-agent"
                        if label["name"] == "ready-for-agent"
                        else label["name"]
                    )
                }
                for label in child["labels"]
            ]
            for comment in child["comments"]:
                if comment["author"]["login"] == "lmpaul":
                    comment["author"] = {"login": "maintainer"}
        self.github_state.write_text(json.dumps(state), encoding="utf-8")
        run(["git", "add", "ralph.toml"], cwd=self.repository)
        run(["git", "commit", "-m", "Configure portable Ralph fixture"], cwd=self.repository)
        environment = self.environment.copy()
        environment["RALPH_TEST_REPOSITORY"] = "acme/widget"

        completed = self._run_loop("191", environment=environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        final_state = json.loads(self.github_state.read_text(encoding="utf-8"))
        child = next(child for child in final_state["children"] if child["number"] == 193)
        self.assertEqual({label["name"] for label in child["labels"]}, {"r-implemented"})
        self.assertEqual(
            {label["name"] for label in final_state["parent"]["labels"]},
            {"r-human-review"},
        )
        github_calls = self._json_lines(self.github_log)
        self.assertTrue(
            any(
                call[0] == "api" and call[1].startswith("repos/acme/widget/")
                for call in github_calls
            )
        )
        tracking = run(
            ["git", "config", "branch.codex/issue-191.remote"],
            cwd=self.repository,
        ).stdout.strip()
        self.assertEqual(tracking, "upstream")

    def test_main_is_rejected_before_codex_starts(self):
        run(["git", "checkout", "main"], cwd=self.repository)

        completed = self._run_loop("191")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("main is not a safe Ralph branch", completed.stderr)
        self.assert_codex_did_not_start()

    def test_detached_head_is_rejected_before_codex_starts(self):
        run(["git", "checkout", "--detach", self.base_sha], cwd=self.repository)

        completed = self._run_loop("191")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("detached HEAD", completed.stderr)
        self.assert_codex_did_not_start()

    def test_dirty_worktree_is_rejected_before_codex_starts(self):
        (self.repository / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")

        completed = self._run_loop("191")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("worktree is dirty at startup", completed.stderr)
        self.assert_codex_did_not_start()

    def test_wrong_repository_is_rejected_before_codex_starts(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_REPOSITORY"] = "someone/else"

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("wrong repository", completed.stderr)
        self.assert_codex_did_not_start()

    def test_child_must_be_ready_for_agent_before_codex_starts(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CHILD_LABELS"] = "ready-for-agent,needs-info"

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("children remaining but none is runnable", completed.stderr)
        self.assert_codex_did_not_start()

    def test_command_must_be_started_from_its_selected_worktree(self):
        completed = self._run_loop("191", cwd=self.fixture_root)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("wrong worktree", completed.stderr)
        self.assert_codex_did_not_start()


if __name__ == "__main__":
    unittest.main()
