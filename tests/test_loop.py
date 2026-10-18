"""Process tests for the standard run; GitHub and Codex remain offline."""

import contextlib
import io
import json
import os
import pathlib
import re
import sys
import unittest
from unittest import mock

from harness import RalphProcessTestCase, run

# Imported from the kit, outside the repository each run is started from.
from ralph_kit import loop


class StartLoopTests(RalphProcessTestCase):
    def run_loop(self, budget=None, **environment):
        settings = self.environment | environment
        args = ("191",) if budget is None else ("191", str(budget))
        return self._run_loop(*args, environment=settings)

    def test_verification_flags_require_toml_booleans(self):
        from ralph_kit.config import RalphConfigError, load_config

        config_path = self.repository / "ralph.toml"
        original = config_path.read_text()
        for key in ("local_checks", "ci_checks"):
            for value in ('"false"', '"true"', '0', '1'):
                with self.subTest(key=key, value=value):
                    config_path.write_text(original.replace(f"{key} = true", f"{key} = {value}"))
                    with self.assertRaisesRegex(RalphConfigError, rf"{key} must be a boolean"):
                        load_config(config_path, self.repository)
        config_path.write_text(original)

    def test_legacy_verification_keys_are_rejected(self):
        from ralph_kit.config import RalphConfigError, load_config

        config_path = self.repository / "ralph.toml"
        config_path.write_text(config_path.read_text().replace(
            "local_checks = true\nci_checks = true",
            'mode = "ci"\nlocal_gates = "enabled"',
        ))
        with self.assertRaisesRegex(RalphConfigError, "unknown keys: local_gates, mode"):
            load_config(config_path, self.repository)

    def calls(self):
        return self._json_lines(self.codex_log)

    def github_calls(self):
        return self._json_lines(self.github_log)

    def state(self):
        return json.loads(self.github_state.read_text())

    def child(self):
        return next(child for child in self.state()["children"] if child["number"] == 193)

    def evidence_files(self, prompt, name):
        return [pathlib.Path(path) for path in re.findall(rf"Full {name}: (.+)", prompt)]

    def assert_completed(self, result, count):
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), count)
        for call in calls:
            self.assertIn("gpt-5.6-terra", call["arguments"])
            self.assertIn('model_reasoning_effort="medium"', call["arguments"])
        self.assertEqual({label["name"] for label in self.child()["labels"]}, {"implemented"})
        self.assertFalse(self.state()["pull_request"]["isDraft"])
        body = self.child()["comments"][-1]["body"]
        self.assertIn("ralph-experimental-implementation-evidence", body)
        self.assertIn("No separate Ralph review session was run", body)
        self.assertNotIn("reviewed_head", body)

    def test_claude_implementation_and_correction_use_the_shared_adapter(self):
        self._configure_engines(implementation="claude", correction="claude")
        result = self.run_loop(RALPH_TEST_FAIL_GATE_COUNT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.codex_log.exists())
        calls = self._json_lines(self.claude_log)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["prompt"].startswith("/implement issue #193\n"))
        self.assertIn("Repair the failing local gate", calls[1]["prompt"])
        self.assertEqual({label["name"] for label in self.child()["labels"]}, {"implemented"})

    def test_local_failure_passes_context_and_both_complete_streams(self):
        self.assert_completed(self.run_loop(RALPH_TEST_FAIL_GATE_COUNT="1"), 2)
        prompt = self.calls()[1]["prompt"]
        self.assertIn("Command: quality-gate", prompt)
        self.assertIn("expected local value 42, received 0", prompt)
        self.assertIn("fixture gate failed", prompt)
        self.assertIn("Parent: https://github.com/lmpaul/buyr/issues/191", prompt)
        self.assertIn("Issue base commit: " + self.base_sha, prompt)
        self.assertIn("Previous session summary: Implemented the requested child issue.", prompt)
        stdout = self.evidence_files(prompt, "stdout")[0].read_text()
        stderr = self.evidence_files(prompt, "stderr")[0].read_text()
        self.assertIn("local progress 50", stdout)
        self.assertIn("last local log line", stdout)
        self.assertNotIn("local progress 50", prompt)
        self.assertIn("[REDACTED]", stderr)
        self.assertNotIn("super-secret", stderr + prompt)
        previous = pathlib.Path(re.search(r"Previous result: (.+)", prompt).group(1))
        self.assertEqual(json.loads(previous.read_text())["outcome"], "committed")
        self.assertEqual(len(self.quality_log.read_text().splitlines()), 2)

    def test_ci_failure_fetches_job_logs_and_rechecks_corrected_commit(self):
        self.assert_completed(self.run_loop(RALPH_TEST_CI_OUTCOMES="failure,success"), 2)
        prompt = self.calls()[1]["prompt"]
        self.assertIn("Repair the failing CI check", prompt)
        self.assertIn("Repository check command: quality-gate", prompt)
        self.assertIn("--job 1001 --log", prompt)
        self.assertIn("expected 42, received 0", prompt)
        stdout = self.evidence_files(prompt, "stdout")[0].read_text()
        self.assertIn("last CI log line", stdout)
        self.assertNotIn("ci-secret", stdout + prompt)
        checks = [call[1] for call in self.github_calls() if call[0] == "api" and "/commits/" in call[1]]
        self.assertEqual(len(checks), 2)
        self.assertNotEqual(checks[0], checks[1])
        self.assertEqual(len(self.quality_log.read_text().splitlines()), 2)

    def test_all_failed_required_checks_reach_one_correction(self):
        self.assert_completed(self.run_loop(
            RALPH_TEST_CI_OUTCOMES="failure,success",
            RALPH_TEST_CHANGED_PATH="buyr-hono/prisma/schema.prisma",
            RALPH_TEST_CI_CHECK_NAMES="Canonical CI,PostgreSQL 16 and Redis acceptance",
        ), 2)
        prompt = self.calls()[1]["prompt"]
        self.assertIn("Canonical CI concluded failure", prompt)
        self.assertIn("PostgreSQL 16 and Redis acceptance concluded failure", prompt)
        self.assertEqual(len(self.evidence_files(prompt, "stdout")), 2)

    def test_expired_logs_leave_explicit_evidence_and_a_job_reference(self):
        self.assert_completed(self.run_loop(
            RALPH_TEST_CI_OUTCOMES="failure,success", START_TEST_LOGS_UNAVAILABLE="1",
        ), 2)
        prompt = self.calls()[1]["prompt"]
        self.assertIn("CI logs unavailable", prompt)
        self.assertIn("Job logs have expired", prompt)
        self.assertIn("/actions/runs/777/job/1001", prompt)

    def test_job_from_another_commit_is_not_used_as_failure_evidence(self):
        self.assert_completed(self.run_loop(
            RALPH_TEST_CI_OUTCOMES="failure,success", START_TEST_STALE_JOB="1",
        ), 2)
        self.assertFalse(any(call[:2] == ["run", "view"] for call in self.github_calls()))
        self.assertIn("does not match the failed commit", self.calls()[1]["prompt"])

    def test_check_without_actions_url_retains_annotations(self):
        self.assert_completed(self.run_loop(
            RALPH_TEST_CI_OUTCOMES="failure,success", START_TEST_NO_JOB_URL="1",
        ), 2)
        prompt = self.calls()[1]["prompt"]
        self.assertIn("CI logs unavailable", prompt)
        self.assertIn("A test assertion failed", prompt)
        self.assertFalse(any(call[:2] == ["run", "view"] for call in self.github_calls()))

    def test_local_and_ci_repairs_chain_previous_context_without_sol(self):
        self.assert_completed(self.run_loop(
            RALPH_TEST_FAIL_GATE_COUNT="1", RALPH_TEST_CI_OUTCOMES="failure,success",
        ), 3)
        self.assertIn("Previous session summary: Repaired the failing local gate.", self.calls()[2]["prompt"])
        self.assertEqual(len(self.quality_log.read_text().splitlines()), 3)

    def test_unfinished_requirements_are_fixed_before_ci(self):
        self.assert_completed(self.run_loop(RALPH_TEST_AGENT_RESULTS=json.dumps([
            {"outcome": "needs_correction", "commit": True, "findings": ["Implement the missing acceptance criterion."]},
            {"outcome": "committed"},
        ])), 2)
        self.assertIn("Implement the missing acceptance criterion.", self.calls()[1]["prompt"])

    def test_local_gate_ci_and_issue_corrections_share_the_correction_limit(self):
        self.assert_completed(self.run_loop(
            RALPH_TEST_FAIL_GATE_COUNT="1",
            RALPH_TEST_CI_OUTCOMES="failure,success",
            RALPH_TEST_AGENT_RESULTS=json.dumps([
                {"outcome": "committed"},
                {"outcome": "committed"},
                {"outcome": "needs_correction", "commit": True, "findings": ["Complete the journey."]},
                {"outcome": "committed"},
            ]),
        ), 4)
        prompts = [call["prompt"] for call in self.calls()]
        self.assertIn("Repair the failing local gate", prompts[1])
        self.assertIn("Correction attempt 1/3", prompts[1])
        self.assertIn("Repair the failing CI check", prompts[2])
        self.assertIn("Correction attempt 2/3", prompts[2])
        self.assertIn("Complete the remaining requirements", prompts[3])
        self.assertIn("Correction attempt 3/3", prompts[3])

    def test_budget_stop_retains_the_local_evidence(self):
        result = self.run_loop(budget=1, RALPH_TEST_FAIL_GATE_COUNT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("Full stdout:", self.child()["comments"][-1]["body"])
        self.assertIsNone(self.state()["pull_request"])

    def test_ci_correction_requires_a_new_commit(self):
        result = self.run_loop(
            RALPH_TEST_CI_OUTCOMES="failure", RALPH_TEST_CI_CORRECTION_NO_CHANGES="1",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CI correction did not create a new commit", result.stderr)
        self.assertEqual({label["name"] for label in self.child()["labels"]}, {"human-review"})

    def test_infrastructure_retries_do_not_launch_a_correction(self):
        self.assert_completed(self.run_loop(RALPH_TEST_CI_OUTCOMES="cancelled,success"), 1)

    def assert_local_only_publication(self, *, local_gates=True):
        calls = self.github_calls()
        self.assertFalse(any(call[0] == "api" and "/commits/" in call[1] for call in calls))
        self.assertTrue(any(call[:2] == ["pr", "create"] and "--draft" in call for call in calls))
        self.assertFalse(any(call[:2] == ["pr", "merge"] for call in calls))
        remote_head = run(
            ["git", "rev-parse", "refs/heads/codex/issue-191"], cwd=self.remote,
        ).stdout.strip()
        local_head = run(["git", "rev-parse", "HEAD"], cwd=self.repository).stdout.strip()
        self.assertEqual(remote_head, local_head)
        body = self.child()["comments"][-1]["body"]
        self.assertIn(
            "remote CI was not required" if local_gates else
            "automated local gates and remote CI were explicitly disabled", body,
        )
        self.assertIn(local_head, body)
        self.assertNotIn("CI are green", body)

    def test_local_only_publishes_with_local_evidence_without_querying_ci(self):
        self._configure_verification(ci_checks=False, local_checks=True)
        self.assert_completed(self.run_loop(), 1)
        self.assertEqual(len(self.quality_log.read_text().splitlines()), 1)
        self.assert_local_only_publication()

    def test_local_only_repairs_failed_gates_before_publication(self):
        self._configure_verification(ci_checks=False, local_checks=True)
        self.assert_completed(self.run_loop(RALPH_TEST_FAIL_GATE_COUNT="1"), 2)
        self.assertIn("Repair the failing local gate", self.calls()[1]["prompt"])
        self.assertEqual(len(self.quality_log.read_text().splitlines()), 2)
        self.assert_local_only_publication()

    def test_local_only_budget_stop_does_not_publish(self):
        self._configure_verification(ci_checks=False, local_checks=True)
        result = self.run_loop(budget=1, RALPH_TEST_FAIL_GATE_COUNT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(self.state()["pull_request"])
        self.assertEqual({label["name"] for label in self.child()["labels"]}, {"human-review"})

    def test_review_profile_is_optional_for_a_standard_run(self):
        config = self.repository / "ralph.toml"
        config.write_text(re.sub(r"\[models.review\].*?(?=\[models.correction\])", "", config.read_text(), flags=re.S))
        run(["git", "commit", "-am", "Remove unused review profile"], cwd=self.repository)
        self.assert_completed(self.run_loop(), 1)

    def test_review_profile_remains_required_for_a_reviewed_run(self):
        config = self.repository / "ralph.toml"
        config.write_text(re.sub(r"\[models.review\].*?(?=\[models.correction\])", "", config.read_text(), flags=re.S))
        run(["git", "commit", "-am", "Remove required review profile"], cwd=self.repository)
        result = run(
            [sys.executable, "-m", "ralph_kit", "run", "--review", "191"],
            cwd=self.repository, env=self.environment, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("configuration.models is missing keys: review", result.stderr)
        self.assertFalse(self.codex_log.exists())

    def disable_local_gates(self, ci_checks):
        self._configure_verification(ci_checks=ci_checks, local_checks=False)
        self.environment["RALPH_TEST_EXPECT_LOCAL_GATES"] = "0"

    def test_local_only_can_disable_all_automated_verification(self):
        self.disable_local_gates(False)
        # An unused policy must not be loaded when both verification paths are off.
        (self.repository / ".github" / "scripts" / "quality.py").unlink()
        (self.repository / ".github" / "quality-gates.json").unlink()
        run(["git", "commit", "-am", "Remove unused quality policy"], cwd=self.repository)
        result = self.run_loop()
        self.assert_completed(result, 1)
        self.assertFalse(self.quality_log.exists())
        self.assertIn("without automated verification", result.stdout)
        self.assert_local_only_publication(local_gates=False)
        body = self.child()["comments"][-1]["body"]
        self.assertIn("automated local gates and remote CI were explicitly disabled", body)
        self.assertNotIn("are green", body)

    def test_ci_can_run_without_local_gates(self):
        self.disable_local_gates(True)
        self.assert_completed(self.run_loop(), 1)
        self.assertFalse(self.quality_log.exists())
        self.assertTrue(any(call[0] == "api" and "/commits/" in call[1] for call in self.github_calls()))
        body = self.child()["comments"][-1]["body"]
        self.assertIn("Canonical CI is green", body)
        self.assertIn("local gates were explicitly disabled", body)
        self.assertNotIn("local gates and canonical CI are green", body)

    def test_ci_failure_is_repaired_when_local_gates_are_disabled(self):
        self.disable_local_gates(True)
        self.assert_completed(self.run_loop(RALPH_TEST_CI_OUTCOMES="failure,success"), 2)
        self.assertIn("Repair the failing CI check", self.calls()[1]["prompt"])
        self.assertFalse(self.quality_log.exists())

    def test_unfinished_requirements_are_repaired_without_automated_verification(self):
        self.disable_local_gates(False)
        self.assert_completed(self.run_loop(RALPH_TEST_AGENT_RESULTS=json.dumps([
            {"outcome": "needs_correction", "commit": True, "findings": ["Implement the missing acceptance criterion."]},
            {"outcome": "committed"},
        ])), 2)
        self.assertIn("Implement the missing acceptance criterion.", self.calls()[1]["prompt"])
        self.assertFalse(self.quality_log.exists())
        self.assert_local_only_publication(local_gates=False)

    def test_recovery_uses_standard_run_evidence_without_another_session(self):
        interrupted = self.run_loop(RALPH_TEST_INTERRUPT_AFTER_EVIDENCE="1")
        self.assertNotEqual(interrupted.returncode, 0)
        self.assert_completed(self.run_loop(), 1)

    def test_blocker_implemented_by_a_reviewed_run_remains_satisfied(self):
        state = self.state()
        blocker = state["children"][0]
        blocker["state"] = "open"
        blocker["labels"] = [{"name": "implemented"}]
        blocker["comments"] = [self._implementation_evidence(self.base_sha)]
        self.github_state.write_text(json.dumps(state))
        self.assert_completed(self.run_loop(), 1)

    def test_reviewed_run_pending_publication_does_not_skip_standard_run_checks(self):
        state = self.state()
        child = state["children"][1]
        evidence = self._implementation_evidence(self.base_sha)
        evidence["createdAt"] = child["updatedAt"]
        child["comments"].append(evidence)
        self.github_state.write_text(json.dumps(state))
        self.assert_completed(self.run_loop(), 1)

    def test_ci_budget_stop_keeps_logs_for_the_human_handoff(self):
        result = self.run_loop(budget=1, RALPH_TEST_CI_OUTCOMES="failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("expected 42, received 0", self.child()["comments"][-1]["body"])
        self.assertIn("Full stdout:", self.child()["comments"][-1]["body"])

    def test_no_changes_still_requires_ci_before_completion(self):
        self.assert_completed(self.run_loop(RALPH_TEST_AGENT_OUTCOME="no_changes"), 1)
        self.assertTrue(any(call[0] == "api" and "/commits/" in call[1] for call in self.github_calls()))

    def test_dirty_agent_output_cannot_be_published(self):
        result = self.run_loop(RALPH_TEST_LEAVE_DIRTY="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("worktree is dirty", result.stderr)
        self.assertIsNone(self.state()["pull_request"])


class RepeatedRunTests(RalphProcessTestCase):
    """Runs started one after the other in one Python process."""

    def run_in_process(self):
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.environment, clear=True),
            contextlib.chdir(self.repository),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            returncode = loop.run(["191"], prog="ralph-kit run")
        self.assertEqual(returncode, 0, stderr.getvalue())

    def test_second_run_does_not_share_the_first_runs_state(self):
        self.run_in_process()
        self.run_in_process()
        first, second = [
            comment["body"]
            for comment in json.loads(self.github_state.read_text())["parent"]["comments"]
            if "ralph-experimental-terminal-summary" in comment["body"]
        ]
        self.assertIn("implemented children this run: 1;", first)
        self.assertIn("agent_sessions: 1;", first)
        self.assertIn("implemented children this run: 0;", second)
        self.assertIn("agent_sessions: 0;", second)
        runs = list((self.repository / ".ralph" / "diagnostics").glob("*/run.json"))
        self.assertEqual(len(runs), 2)


if __name__ == "__main__":
    unittest.main()
