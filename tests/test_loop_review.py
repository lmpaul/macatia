import json
import os
import pathlib
import re
import unittest
from unittest import mock

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

    def test_claude_implementation_with_codex_review(self):
        self._configure_engines(implementation="claude")
        result = self._run_loop("191")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self._json_lines(self.claude_log)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self._json_lines(self.codex_log)), 1)
        self.assertIn("claude-implementation", calls[0]["arguments"])
        self.assertIn("--effort", calls[0]["arguments"])
        self.assertIn("--json-schema", calls[0]["arguments"])
        arguments = calls[0]["arguments"]
        schema = json.loads(arguments[arguments.index("--json-schema") + 1])
        self.assertNotIn("$schema", schema)
        self.assertEqual(schema["required"], ["outcome", "summary", "findings"])
        self.assertTrue(calls[0]["prompt"].startswith("/implement issue #193\n"))
        self.assertNotIn("Read .agents/skills/implement", calls[0]["prompt"])
        self.assertNotIn("$implement", calls[0]["prompt"])
        records = self._diagnostic_records()
        self.assertEqual([record["engine"] for record in records], ["claude", "codex"])
        self.assertEqual(records[0]["structured_result"]["outcome"], "committed")

    def test_all_roles_can_use_claude_including_gate_correction(self):
        self._configure_engines(implementation="claude", review="claude", correction="claude")
        result = self._run_loop("191", environment=self.environment | {"RALPH_TEST_FAIL_GATE_COUNT": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.codex_log.exists())
        calls = self._json_lines(self.claude_log)
        self.assertEqual(len(calls), 3)
        self.assertIn("Repair the failing local gate", calls[1]["prompt"])
        self.assertIn("Review the exact", calls[2]["prompt"])
        self.assertEqual({record["engine"] for record in self._diagnostic_records()}, {"claude"})

    def test_claude_errors_and_invalid_results_do_not_publish_work(self):
        self._configure_engines(implementation="claude")
        result = self._run_loop("191", environment=self.environment | {"RALPH_TEST_CLAUDE_ERROR": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Claude Code reported an error", result.stderr)
        self.assert_only_safe_terminal_summary_was_published()
        self.assertEqual(len(self._diagnostic_records()), 1)
        self.assertEqual(self._diagnostic_records()[0]["returncode"], 0)

    def test_invalid_claude_json_stops_before_review(self):
        self._configure_engines(implementation="claude")
        result = self._run_loop("191", environment=self.environment | {"RALPH_TEST_CLAUDE_INVALID_JSON": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Claude Code did not return valid JSON", result.stderr)
        self.assertFalse(self.codex_log.exists())
        self.assert_only_safe_terminal_summary_was_published()

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

    def test_claude_output_still_requires_the_ralph_contract(self):
        self._configure_engines(implementation="claude")
        result = self._run_loop("191", environment=self.environment | {
            "RALPH_TEST_CLAUDE_RESULT_OVERRIDE": json.dumps({"outcome": "committed", "summary": "Done"}),
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match the Ralph contract", result.stderr)
        self.assert_only_safe_terminal_summary_was_published()

    def test_missing_claude_structured_result_stops_before_review(self):
        self._configure_engines(implementation="claude")
        result = self._run_loop("191", environment=self.environment | {"RALPH_TEST_INVALID_OUTPUT": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not return a structured_output object", result.stderr)
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

    def test_legacy_config_without_engine_still_uses_codex(self):
        path = self.repository / "ralph.toml"
        path.write_text(path.read_text().replace('engine = "codex"\n', ''))
        run(["git", "add", "ralph.toml"], cwd=self.repository)
        run(["git", "commit", "-m", "Legacy configuration"], cwd=self.repository)
        result = self._run_loop("191")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 2)
        self.assertFalse(self.claude_log.exists())

    def test_default_ci_poll_interval_is_45_seconds(self):
        from ralph_kit import loop_core as module

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(module.ci_poll_seconds(), 45.0)

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

    def test_local_only_uses_local_gates_without_querying_ci(self):
        self._configure_verification(ci_checks=False, local_checks=True)

        completed = self._run_loop("191")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        github_calls = self._json_lines(self.github_log)
        self.assertFalse(
            any(call[0] == "api" and "/check-runs" in call[1] for call in github_calls)
        )
        self.assertEqual(len(self.quality_log.read_text().splitlines()), 2)
        child = next(
            child
            for child in json.loads(self.github_state.read_text())["children"]
            if child["number"] == 193
        )
        self.assertEqual({label["name"] for label in child["labels"]}, {"implemented"})
        self.assertIn("remote CI was not required", child["comments"][-1]["body"])

    def test_local_only_can_explicitly_disable_all_automated_verification(self):
        self._configure_verification(ci_checks=False, local_checks=False)
        environment = self.environment.copy()
        environment["RALPH_TEST_EXPECT_LOCAL_GATES"] = "0"

        completed = self._run_loop("191", environment=environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("without automated verification", completed.stdout)
        self.assertIn("Local quality gates are disabled", completed.stdout)
        self.assertFalse(self.quality_log.exists())
        github_calls = self._json_lines(self.github_log)
        self.assertFalse(
            any(call[0] == "api" and "/check-runs" in call[1] for call in github_calls)
        )
        child = next(
            child
            for child in json.loads(self.github_state.read_text())["children"]
            if child["number"] == 193
        )
        self.assertEqual({label["name"] for label in child["labels"]}, {"implemented"})
        self.assertIn(
            "automated local gates and remote CI were explicitly disabled",
            child["comments"][-1]["body"],
        )

    def test_ci_can_run_when_local_gates_are_disabled(self):
        self._configure_verification(ci_checks=True, local_checks=False)
        environment = self.environment.copy()
        environment["RALPH_TEST_EXPECT_LOCAL_GATES"] = "0"

        completed = self._run_loop("191", environment=environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(self.quality_log.exists())
        self.assertTrue(
            any(
                call[0] == "api" and "/check-runs" in call[1]
                for call in self._json_lines(self.github_log)
            )
        )
        child = next(
            child
            for child in json.loads(self.github_state.read_text())["children"]
            if child["number"] == 193
        )
        self.assertIn(
            "local gates were explicitly disabled", child["comments"][-1]["body"]
        )

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

    def test_claimed_commit_without_a_real_git_commit_is_rejected(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_TERRA_SKIPS_COMMIT"] = "1"

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("claimed committed without creating a Git commit", completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 1)
        state = json.loads(self.github_state.read_text())
        child = next(child for child in state["children"] if child["number"] == 193)
        self.assertEqual({label["name"] for label in child["labels"]}, {"human-review"})
        self.assertIn(
            "claimed committed without creating a Git commit",
            child["comments"][-1]["body"],
        )
        remote_branch = run(
            ["git", "rev-parse", "--verify", "refs/remotes/origin/codex/issue-191"],
            cwd=self.repository,
            check=False,
        )
        self.assertNotEqual(remote_branch.returncode, 0)

    def test_invalid_structured_output_stops_without_publishing_and_is_retained(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_INVALID_OUTPUT"] = "1"

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("agent did not return valid structured output", completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 1)
        self.assert_only_safe_terminal_summary_was_published()
        record = self._diagnostic_records()[0]
        self.assertIsNone(record["structured_result"])
        self.assertIn("agent did not return valid structured output", (self.repository / ".ralph" / "diagnostics" / record["run_id"] / "sessions" / "001-implementation" / "validation.txt").read_text())

    def test_dirty_agent_worktree_stops_without_discarding_diagnostic_evidence(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_LEAVE_DIRTY"] = "1"

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("worktree is dirty after implementation", completed.stderr)
        self.assertTrue((self.repository / "uncommitted-agent-output.txt").is_file())
        record = self._diagnostic_records()[0]
        self.assertEqual(record["structured_result"]["outcome"], "committed")
        self.assertIn("worktree is dirty after implementation", (self.repository / ".ralph" / "diagnostics" / record["run_id"] / "sessions" / "001-implementation" / "validation.txt").read_text())

    def test_nonzero_codex_exit_with_valid_committed_result_is_still_verified(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CODEX_NONZERO"] = "1"

        completed = self._run_loop("191", environment=environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        records = self._diagnostic_records()
        self.assertEqual(records[0]["returncode"], 1)
        self.assertEqual(records[0]["structured_result"]["outcome"], "committed")

    def test_nonzero_codex_exit_with_blocked_result_cannot_change_issue_state(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_AGENT_OUTCOME"] = "blocked"
        environment["RALPH_TEST_CODEX_NONZERO"] = "1"

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("exited non-zero without a committed outcome", completed.stderr)
        child = next(
            child
            for child in json.loads(self.github_state.read_text())["children"]
            if child["number"] == 193
        )
        self.assertEqual({label["name"] for label in child["labels"]}, {"ready-for-agent"})

    def test_blocked_ci_correction_leaves_precise_human_review_evidence(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CI_OUTCOMES"] = "failure"
        environment["RALPH_TEST_CORRECTION_OUTCOME"] = "blocked"

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("CI correction stopped with blocked", completed.stderr)
        child = next(
            child
            for child in json.loads(self.github_state.read_text())["children"]
            if child["number"] == 193
        )
        self.assertEqual({label["name"] for label in child["labels"]}, {"human-review"})
        self.assertIn("CI correction reported blocked", child["comments"][-1]["body"])

    def test_no_changes_requires_an_independent_clean_review_before_publishing(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_AGENT_OUTCOME"] = "no_changes"
        environment["RALPH_TEST_SUMMARY"] = "No further implementation is required."
        environment["RALPH_TEST_SOL_SUMMARY"] = "The reviewed diff requires no changes."

        completed = self._run_loop("191", environment=environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 2)
        self.assertEqual(
            run(["git", "rev-parse", "HEAD"], cwd=self.repository).stdout.strip(),
            self.base_sha,
        )
        child = next(
            child
            for child in json.loads(self.github_state.read_text())["children"]
            if child["number"] == 193
        )
        self.assertEqual({label["name"] for label in child["labels"]}, {"implemented"})

    def test_run_budget_is_checked_before_each_agent_session(self):
        completed = self._run_loop("191", "1")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("run budget exhausted before review", completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 1)

    def test_local_gate_failure_starts_a_fresh_correction_and_rechecks_it(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_FAIL_GATE_COUNT"] = "1"

        completed = self._run_loop("191", environment=environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self._json_lines(self.codex_log)
        self.assertEqual(len(calls), 3)
        self.assertIn("Repair the failing local gate", calls[1]["prompt"])
        self.assertIn(
            "Use an empty findings array for committed or no_changes.",
            calls[1]["prompt"],
        )
        self.assertNotIn("$implement", calls[1]["prompt"])
        self.assertIn(
            "model_reasoning_effort=\"medium\"", calls[1]["arguments"]
        )
        self.assertIn(
            "🤖 Starting Codex local gate correction session (Terra - Medium) "
            "for child #193.",
            completed.stdout,
        )
        self.assertIn(
            "🤖 Codex local gate correction session (Terra - Medium) finished "
            "for child #193 (outcome: committed; commits: 1).",
            completed.stdout,
        )
        self.assertNotIn("super-secret", calls[1]["prompt"])
        self.assertNotIn("hidden-token", calls[1]["prompt"])
        self.assertNotIn("bearer-secret", calls[1]["prompt"])
        self.assertNotIn("user:password", calls[1]["prompt"])
        self.assertNotIn("```escaped fence", calls[1]["prompt"])
        self.assertEqual(len(self.quality_log.read_text().splitlines()), 3)

    def test_local_gate_correction_receives_complete_evidence_and_previous_context(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_FAIL_GATE_COUNT"] = "1"

        completed = self._run_loop("191", environment=environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self._json_lines(self.codex_log)
        prompt = calls[1]["prompt"]
        self.assertIn("Command: quality-gate", prompt)
        self.assertIn("Exit code: 1", prompt)
        self.assertIn("AssertionError: expected local value 42, received 0", prompt)
        self.assertIn("fixture gate failed:", prompt)
        self.assertIn("Previous session summary: Implemented the requested child issue.", prompt)
        self.assertIn("Previous result:", prompt)
        stdout = pathlib.Path(re.search(r"Full stdout: (.+)", prompt).group(1))
        stderr = pathlib.Path(re.search(r"Full stderr: (.+)", prompt).group(1))
        self.assertIn("last local log line", stdout.read_text())
        self.assertIn("[REDACTED]", stderr.read_text())
        self.assertNotIn("super-secret", prompt + stderr.read_text())
        self.assertIn("Previous session summary: Repaired the failing local gate.", calls[2]["prompt"])

    def test_completed_work_in_local_correction_findings_has_an_actionable_error(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_FAIL_GATE_COUNT"] = "1"
        environment["RALPH_TEST_SUMMARY"] = (
            "Repaired and committed the persistence assertion mismatch in 2ee9b45."
        )
        environment["RALPH_TEST_AGENT_RESULTS"] = json.dumps([
            {"outcome": "committed"},
            {
                "outcome": "committed",
                "findings": [
                    "Updated Ad report fixture lookups to use providerId after the durable internal Ad ID change.",
                    "Focused persistence test passed; persistence and migration/Meta acceptance gates completed.",
                    "No push or GitHub state changes made.",
                ],
            },
        ])

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("committed requires an empty findings array", completed.stderr)
        self.assertIn("completed work and successful checks in summary", completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 2)
        self.assert_only_safe_terminal_summary_was_published()

    def test_repeated_local_gate_failure_exhausts_the_run_budget(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_FAIL_GATE_COUNT"] = "99"

        completed = self._run_loop("191", "3", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("run budget exhausted before local gate correction", completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 3)
        self.assertFalse(
            any(
                call[:2] == ["issue", "edit"] and "implemented" in call
                for call in self._json_lines(self.github_log)
            )
        )
        state = json.loads(self.github_state.read_text(encoding="utf-8"))
        child = next(child for child in state["children"] if child["number"] == 193)
        self.assertEqual(
            {label["name"] for label in child["labels"]}, {"human-review"}
        )
        self.assertIn("run budget of 3 agent sessions", child["comments"][-1]["body"])
        self.assertNotIn("super-secret", child["comments"][-1]["body"])
        self.assertNotIn("hidden-token", child["comments"][-1]["body"])

    def test_default_budget_allows_five_sessions_per_child(self):
        self._write_github_state(children=[self._child(193)], blockers={})
        environment = self.environment.copy()
        finding = "Expose freshness for each dossier section."
        environment["RALPH_TEST_AGENT_RESULTS"] = json.dumps([
            {"outcome": "committed"},
            {"outcome": "needs_correction", "findings": [finding]},
            {"outcome": "committed"},
            {"outcome": "needs_correction", "findings": [finding]},
            {"outcome": "committed"},
        ])

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("run budget exhausted before review", completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 5)

    def test_blocked_agent_leaves_precise_human_review_evidence(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_AGENT_OUTCOME"] = "blocked"

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("implementation stopped with blocked", completed.stderr)
        state = json.loads(self.github_state.read_text(encoding="utf-8"))
        child = next(child for child in state["children"] if child["number"] == 193)
        self.assertEqual({label["name"] for label in child["labels"]}, {"human-review"})
        self.assertIn("implementation reported blocked", child["comments"][-1]["body"])
        self.assertFalse(
            any(
                call[:2] == ["issue", "edit"] and "implemented" in call
                for call in self._json_lines(self.github_log)
            )
        )

    def test_partial_implementation_is_corrected_and_reviewed_before_publication(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_SUMMARY"] = "x" * 501
        finding = "Add previous/next navigation within the filtered Prospect list."
        environment["RALPH_TEST_AGENT_RESULTS"] = json.dumps([
            {"outcome": "needs_correction", "commit": True, "findings": [finding]},
            {"outcome": "committed"},
            {"outcome": "no_changes"},
        ])

        completed = self._run_loop("191", environment=environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self._json_lines(self.codex_log)
        self.assertEqual(len(calls), 3)
        self.assertIn("Complete the remaining requirements", calls[1]["prompt"])
        self.assertIn(finding, calls[1]["prompt"])
        self.assertIn("gpt-5.6-sol", calls[2]["arguments"])
        head = run(["git", "rev-parse", "HEAD"], cwd=self.repository).stdout.strip()
        self.assertIn(head, calls[2]["prompt"])
        state = json.loads(self.github_state.read_text())
        child = next(child for child in state["children"] if child["number"] == 193)
        self.assertEqual({label["name"] for label in child["labels"]}, {"implemented"})
        self.assertIn(head, child["comments"][-1]["body"])
        self.assertTrue(any(
            call[0] == "api" and f"/commits/{head}/check-runs" in call[1]
            for call in self._json_lines(self.github_log)
        ))

    def test_review_findings_require_correction_and_a_new_independent_review(self):
        environment = self.environment.copy()
        finding = "Retain unknown-date Touchpoints at the end of the observed journey."
        environment["RALPH_TEST_AGENT_RESULTS"] = json.dumps([
            {"outcome": "committed"},
            {"outcome": "needs_correction", "findings": [finding]},
            {"outcome": "committed"},
            {"outcome": "blocked", "findings": ["The corrected journey still needs review."]},
        ])

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("review stopped with blocked", completed.stderr)
        calls = self._json_lines(self.codex_log)
        self.assertEqual(len(calls), 4)
        self.assertIn(finding, calls[2]["prompt"])
        self.assertIn("gpt-5.6-sol", calls[3]["arguments"])
        state = json.loads(self.github_state.read_text())
        child = next(child for child in state["children"] if child["number"] == 193)
        self.assertEqual({label["name"] for label in child["labels"]}, {"human-review"})
        self.assertFalse(any(
            call[:2] == ["pr", "create"] or (call[0] == "api" and "/check-runs" in call[1])
            for call in self._json_lines(self.github_log)
        ))

    def test_issue_correction_budget_exhaustion_preserves_remaining_findings(self):
        environment = self.environment.copy()
        finding = "Add reversible journey ordering."
        environment["RALPH_TEST_AGENT_RESULTS"] = json.dumps([
            {"outcome": "needs_correction", "commit": True, "findings": [finding]},
        ])

        completed = self._run_loop("191", "1", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("run budget exhausted before issue correction", completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 1)
        state = json.loads(self.github_state.read_text())
        child = next(child for child in state["children"] if child["number"] == 193)
        self.assertEqual({label["name"] for label in child["labels"]}, {"human-review"})
        self.assertIn(finding, child["comments"][-1]["body"])

    def test_issue_correction_without_progress_stops_for_human_review(self):
        environment = self.environment.copy()
        finding = "Retain unknown-date Touchpoints."
        environment["RALPH_TEST_AGENT_RESULTS"] = json.dumps([
            {"outcome": "needs_correction", "commit": True, "findings": [finding]},
            {"outcome": "no_changes"},
        ])

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Issue correction did not create a new commit", completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 2)
        state = json.loads(self.github_state.read_text())
        child = next(child for child in state["children"] if child["number"] == 193)
        self.assertEqual({label["name"] for label in child["labels"]}, {"human-review"})
        self.assertIn(finding, child["comments"][-1]["body"])

    def test_failed_issue_correction_handoff_keeps_the_original_findings(self):
        environment = self.environment.copy()
        finding = "Expose freshness for every dossier section."
        environment["RALPH_TEST_AGENT_RESULTS"] = json.dumps([
            {"outcome": "needs_correction", "commit": True, "findings": [finding]},
            {"outcome": "failed"},
        ])

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("issue correction stopped with failed", completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 2)
        state = json.loads(self.github_state.read_text())
        child = next(child for child in state["children"] if child["number"] == 193)
        self.assertEqual({label["name"] for label in child["labels"]}, {"human-review"})
        self.assertIn(finding, child["comments"][-1]["body"])

    def test_ci_correction_without_progress_handoff_keeps_the_findings(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CI_OUTCOMES"] = "failure"
        finding = "The CI repair still needs section freshness."
        environment["RALPH_TEST_AGENT_RESULTS"] = json.dumps([
            {"outcome": "committed"},
            {"outcome": "no_changes"},
            {"outcome": "needs_correction", "findings": [finding]},
        ])

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("CI correction did not create a new commit", completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 3)
        state = json.loads(self.github_state.read_text())
        child = next(child for child in state["children"] if child["number"] == 193)
        self.assertEqual({label["name"] for label in child["labels"]}, {"human-review"})
        self.assertIn(finding, child["comments"][-1]["body"])

    def test_needs_correction_without_findings_cannot_authorize_publication(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_AGENT_RESULTS"] = json.dumps([
            {"outcome": "needs_correction", "commit": True},
        ])

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("agent structured output does not match the Ralph contract", completed.stderr)
        self.assert_only_safe_terminal_summary_was_published()

    def test_committed_with_remaining_findings_cannot_authorize_publication(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_AGENT_RESULTS"] = json.dumps([
            {"outcome": "committed", "findings": ["Section freshness is missing."]},
        ])

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("agent structured output does not match the Ralph contract", completed.stderr)
        self.assert_only_safe_terminal_summary_was_published()

    def test_failed_agent_with_a_commit_remains_a_terminal_stop(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_AGENT_RESULTS"] = json.dumps([
            {"outcome": "failed", "commit": True},
        ])

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("implementation stopped with failed", completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 1)
        state = json.loads(self.github_state.read_text())
        child = next(child for child in state["children"] if child["number"] == 193)
        self.assertEqual({label["name"] for label in child["labels"]}, {"human-review"})

    def test_findings_are_preserved_in_full_and_redacted_for_correction_and_handoff(self):
        environment = self.environment.copy()
        finding = "First requirement.\n" + "Section details. " * 100 + "\nLast requirement. token=fixture-secret"
        environment["RALPH_TEST_AGENT_RESULTS"] = json.dumps([
            {"outcome": "needs_correction", "commit": True, "findings": [finding]},
            {"outcome": "no_changes"},
        ])

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        calls = self._json_lines(self.codex_log)
        state = json.loads(self.github_state.read_text())
        child = next(child for child in state["children"] if child["number"] == 193)
        for diagnostic in (calls[1]["prompt"], child["comments"][-1]["body"]):
            self.assertIn("First requirement.", diagnostic)
            self.assertIn("Section details. " * 100, diagnostic)
            self.assertIn("Last requirement.", diagnostic)
            self.assertIn("[REDACTED]", diagnostic)
            self.assertNotIn("fixture-secret", diagnostic)

    def test_nonzero_needs_correction_is_not_retried_as_an_issue_gap(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CODEX_NONZERO"] = "1"
        environment["RALPH_TEST_AGENT_RESULTS"] = json.dumps([
            {"outcome": "needs_correction", "commit": True, "findings": ["Complete the journey."]},
        ])

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("exited non-zero without a committed outcome", completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 1)
        self.assert_only_safe_terminal_summary_was_published()

    def test_explicit_budget_replaces_the_larger_default_across_children(self):
        self._write_github_state(
            children=[self._child(193), self._child(194)],
            blockers={},
        )

        completed = self._run_loop("191", "3")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("run budget exhausted before review", completed.stderr)
        calls = self._json_lines(self.codex_log)
        self.assertEqual(len(calls), 3)
        self.assertIn("issue #193", calls[0]["prompt"])
        self.assertIn("issue #194", calls[2]["prompt"])
        self.assertTrue(
            all("issue #194" not in call["prompt"] for call in calls[0:2])
        )

    def test_more_than_two_summary_sentences_do_not_stop_a_verified_run(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_SUMMARY"] = "One sentence. Two sentences. Three sentences."

        completed = self._run_loop("191", environment=environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 2)

    def test_long_summary_is_replaced_after_structured_output_validation(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_SUMMARY"] = "x" * 501

        completed = self._run_loop("191", environment=environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        records = self._diagnostic_records()
        self.assertEqual(len(records), 2)
        self.assertTrue(
            all(
                record["structured_result"]["summary"]
                == "Agent completed the session; detailed output is retained in "
                "Ralph diagnostics."
                for record in records
            )
        )

    def test_branch_advance_after_ci_cannot_authorize_implemented(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_ADVANCE_AFTER_CI"] = "1"

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("branch advanced after canonical CI", completed.stderr)
        github_calls = self._json_lines(self.github_log)
        self.assertFalse(
            any(call[:2] == ["issue", "edit"] for call in github_calls)
        )

    def test_stale_ci_evidence_cannot_authorize_implemented(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CI_OUTCOMES"] = "stale"
        environment["RALPH_TEST_STALE_CI_HEAD"] = self.base_sha

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("infrastructure retries exhausted", completed.stderr)
        self.assertFalse(
            any(
                call[:2] == ["issue", "edit"] and "implemented" in call
                for call in self._json_lines(self.github_log)
            )
        )

    def test_frontend_only_change_does_not_require_the_database_check(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CHANGED_PATH"] = "buyr-web/src/app/page.tsx"
        environment["RALPH_TEST_CI_CHECK_NAMES"] = (
            "Canonical CI,PostgreSQL 16 and Redis acceptance"
        )
        environment["RALPH_TEST_CI_FAILED_CHECK"] = (
            "PostgreSQL 16 and Redis acceptance"
        )

        completed = self._run_loop("191", environment=environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 2)

    def test_persistence_change_requires_a_green_database_check_for_its_sha(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CHANGED_PATH"] = "buyr-hono/prisma/schema.prisma"
        environment["RALPH_TEST_CI_CHECK_NAMES"] = "Canonical CI"

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "no required current-SHA check evidence is available for: "
            "PostgreSQL 16 and Redis acceptance",
            completed.stderr,
        )
        self.assertFalse(
            any(
                call[:2] == ["issue", "edit"] and "implemented" in call
                for call in self._json_lines(self.github_log)
            )
        )

    def test_persistence_change_rejects_failed_database_evidence(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CHANGED_PATH"] = "buyr-hono/prisma/schema.prisma"
        environment["RALPH_TEST_CI_CHECK_NAMES"] = (
            "Canonical CI,PostgreSQL 16 and Redis acceptance"
        )
        environment["RALPH_TEST_CI_FAILED_CHECK"] = (
            "PostgreSQL 16 and Redis acceptance"
        )

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "PostgreSQL 16 and Redis acceptance concluded failure",
            self._json_lines(self.codex_log)[2]["prompt"],
        )
        self.assertFalse(
            any(
                call[:2] == ["issue", "edit"] and "implemented" in call
                for call in self._json_lines(self.github_log)
            )
        )

    def test_persistence_change_rejects_skipped_database_evidence(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CHANGED_PATH"] = "buyr-hono/prisma/schema.prisma"
        environment["RALPH_TEST_CI_CHECK_NAMES"] = (
            "Canonical CI,PostgreSQL 16 and Redis acceptance"
        )
        environment["RALPH_TEST_CI_CHECK_CONCLUSIONS"] = json.dumps(
            {"PostgreSQL 16 and Redis acceptance": "skipped"}
        )

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "PostgreSQL 16 and Redis acceptance concluded skipped",
            self._json_lines(self.codex_log)[2]["prompt"],
        )
        self.assertFalse(
            any(
                call[:2] == ["issue", "edit"] and "implemented" in call
                for call in self._json_lines(self.github_log)
            )
        )

    def test_persistence_change_rejects_cancelled_database_evidence(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CHANGED_PATH"] = "buyr-hono/prisma/schema.prisma"
        environment["RALPH_TEST_CI_CHECK_NAMES"] = (
            "Canonical CI,PostgreSQL 16 and Redis acceptance"
        )
        environment["RALPH_TEST_CI_CHECK_CONCLUSIONS"] = json.dumps(
            {"PostgreSQL 16 and Redis acceptance": "cancelled"}
        )

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("infrastructure retries exhausted", completed.stderr)

    def test_persistence_change_rejects_database_evidence_from_another_sha(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CHANGED_PATH"] = "buyr-hono/prisma/schema.prisma"
        environment["RALPH_TEST_CI_CHECK_NAMES"] = (
            "Canonical CI,PostgreSQL 16 and Redis acceptance"
        )
        environment["RALPH_TEST_STALE_CI_CHECK"] = (
            "PostgreSQL 16 and Redis acceptance"
        )
        environment["RALPH_TEST_STALE_CI_HEAD"] = self.base_sha

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("infrastructure retries exhausted", completed.stderr)

    def test_product_ci_failure_starts_a_fresh_correction_and_waits_for_its_sha(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CI_OUTCOMES"] = "failure,success"

        completed = self._run_loop("191", environment=environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self._json_lines(self.codex_log)
        self.assertEqual(len(calls), 4)
        self.assertIn("Repair the failing CI check", calls[2]["prompt"])
        self.assertIn("Canonical CI concluded failure", calls[2]["prompt"])
        self.assertIn("gpt-5.6-sol", calls[3]["arguments"])
        head = run(["git", "rev-parse", "HEAD"], cwd=self.repository).stdout.strip()
        check_calls = [
            call for call in self._json_lines(self.github_log)
            if call[0] == "api" and "/check-runs" in call[1]
        ]
        self.assertEqual(len(check_calls), 2)
        self.assertIn(f"/commits/{head}/check-runs", check_calls[-1][1])
        records = self._diagnostic_records()
        self.assertIn(records[2]["observed_head"], calls[3]["prompt"])
        self.assertNotEqual(
            check_calls[0][1].split("/commits/", 1)[1].split("/", 1)[0],
            check_calls[1][1].split("/commits/", 1)[1].split("/", 1)[0],
        )

    def test_ci_correction_receives_all_failed_checks_and_verified_job_logs(self):
        environment = self.environment.copy()
        environment.update({
            "RALPH_TEST_CI_OUTCOMES": "failure,success",
            "RALPH_TEST_CHANGED_PATH": "buyr-hono/prisma/schema.prisma",
            "RALPH_TEST_CI_CHECK_NAMES": "Canonical CI,PostgreSQL 16 and Redis acceptance",
        })

        completed = self._run_loop("191", environment=environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self._json_lines(self.codex_log)
        prompt = calls[2]["prompt"]
        self.assertIn("Canonical CI concluded failure", prompt)
        self.assertIn("PostgreSQL 16 and Redis acceptance concluded failure", prompt)
        self.assertIn("Repository check command: quality-gate", prompt)
        self.assertIn("expected 42, received 0", prompt)
        self.assertEqual(prompt.count("Full stdout:"), 2)
        self.assertNotIn("ci-secret", prompt)
        for path in re.findall(r"Full stdout: (.+)", prompt):
            evidence = pathlib.Path(path).read_text()
            self.assertIn("last CI log line", evidence)
            self.assertNotIn("ci-secret", evidence)
        self.assertIn("Previous session summary:", calls[3]["prompt"])

    def test_ci_job_from_another_commit_is_not_used_as_evidence(self):
        environment = self.environment.copy()
        environment.update({
            "RALPH_TEST_CI_OUTCOMES": "failure,success",
            "START_TEST_STALE_JOB": "1",
        })

        completed = self._run_loop("191", environment=environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("does not match the failed commit", self._json_lines(self.codex_log)[2]["prompt"])
        self.assertFalse(any(call[:2] == ["run", "view"] for call in self._json_lines(self.github_log)))

    def test_cancelled_and_timed_out_ci_runs_retry_as_infrastructure_failures(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CI_OUTCOMES"] = "cancelled,timed_out"

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("infrastructure retries exhausted", completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 2)
        child = next(
            child
            for child in json.loads(self.github_state.read_text())["children"]
            if child["number"] == 193
        )
        self.assertEqual(
            {label["name"] for label in child["labels"]}, {"human-review"}
        )

    def test_unavailable_ci_retries_without_another_agent_session(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CI_OUTCOMES"] = "unavailable"

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("infrastructure retries exhausted", completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 2)
        github_calls = self._json_lines(self.github_log)
        self.assertTrue(
            any(
                call[:3] == ["issue", "comment", "191"]
                and "Ralph summary:" in call[-1]
                and "final outcome: stopped safely." in call[-1]
                for call in github_calls
            )
        )
        self.assertTrue(
            any(
                call[:3] == ["pr", "comment", "1"]
                and "final outcome: stopped safely." in call[-1]
                for call in github_calls
            )
        )
        self.assertFalse(any(call[:2] == ["pr", "ready"] for call in github_calls))

    def test_authentication_and_service_outages_are_infrastructure_failures(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CI_OUTCOMES"] = "authentication_failed,service_outage"

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("infrastructure retries exhausted", completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 2)

    def test_infrastructure_retry_can_succeed_without_consuming_the_run_budget(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CI_OUTCOMES"] = "cancelled,success"

        completed = self._run_loop("191", environment=environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 2)
        self.assertIn("1 infrastructure retries", completed.stdout)
        self.assertIn(
            "⏳ Waiting 0 seconds for GitHub Actions to start.", completed.stdout
        )
        self.assertIn("🔎 Checking canonical CI (attempt 1/4).", completed.stdout)
        self.assertIn(
            "⚠️ Canonical CI is not ready; the next check is in 0 seconds.",
            completed.stdout,
        )
        self.assertIn("🔎 Checking canonical CI (attempt 2/4).", completed.stdout)

    def test_ci_correction_without_a_commit_stops_before_rechecking_failed_sha(self):
        environment = self.environment.copy()
        environment["RALPH_TEST_CI_OUTCOMES"] = "failure"
        environment["RALPH_TEST_CI_CORRECTION_NO_CHANGES"] = "1"

        completed = self._run_loop("191", environment=environment)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("CI correction did not create a new commit", completed.stderr)
        self.assertEqual(len(self._json_lines(self.codex_log)), 3)
        self.assertEqual(
            sum(
                call[0] == "api" and "/check-runs" in call[1]
                for call in self._json_lines(self.github_log)
            ),
            1,
        )
        child = next(
            child
            for child in json.loads(self.github_state.read_text())["children"]
            if child["number"] == 193
        )
        self.assertEqual(
            {label["name"] for label in child["labels"]}, {"human-review"}
        )


if __name__ == "__main__":
    unittest.main()
