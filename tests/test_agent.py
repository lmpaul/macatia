"""Engine command translation and result parsing against outputs recorded from the real CLIs."""

import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


KIT_ROOT = pathlib.Path(__file__).resolve().parents[1]
ENGINE_FIXTURES = KIT_ROOT / "tests" / "fixtures" / "engines"
sys.path.insert(0, str(KIT_ROOT / "src"))

from live_support import FORBIDDEN_FIXTURE_MARKERS
from macatia.agent import agent_command, session_prompt, write_agent_result
from macatia.config import AgentProfile
from macatia.loop_core import OUTCOME_SCHEMA_PATH, MacatiaError, parse_agent_result


RECORDED = json.loads((ENGINE_FIXTURES / "recorded.json").read_text(encoding="utf-8"))
CLAUDE_STDOUT = (ENGINE_FIXTURES / RECORDED["claude"]["output"]).read_text(encoding="utf-8")
CODEX_RESULT = (ENGINE_FIXTURES / RECORDED["codex"]["output"]).read_text(encoding="utf-8")
REPOSITORY = pathlib.Path("/work/repository")
OUTPUT = pathlib.Path("/work/result.json")
SYSTEM_PROMPT = (
    "Read and follow AGENTS.md in the repository and applicable directories. "
    "Work in the current repository; leave pushing and GitHub mutations to Macatia."
)


class NoSubprocessTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            subprocess, "Popen", side_effect=AssertionError("engine tests must not start a process")
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class RecordedFixtureTests(unittest.TestCase):
    def test_each_engine_fixture_names_the_cli_version_that_recorded_it(self):
        self.assertRegex(RECORDED["claude"]["version"], r"^\d+\.\d+\.\d+ \(Claude Code\)$")
        self.assertRegex(RECORDED["codex"]["version"], r"^codex-cli \d+\.\d+\.\d+$")

    def test_fixtures_hold_no_machine_paths_or_credentials(self):
        recorded = (ENGINE_FIXTURES / "recorded.json").read_text(encoding="utf-8")
        for text in (CLAUDE_STDOUT, CODEX_RESULT, recorded):
            for marker in FORBIDDEN_FIXTURE_MARKERS:
                self.assertNotIn(marker, text)


class CommandTranslationTests(NoSubprocessTestCase):
    def test_codex_command_carries_the_profile_schema_result_file_and_repository(self):
        profile = AgentProfile("gpt-5.6-terra", "GPT-5.6 Terra", "high", "codex")

        self.assertEqual(
            agent_command(profile, "$implement issue #7", REPOSITORY, OUTCOME_SCHEMA_PATH, OUTPUT),
            [
                "codex", "exec", "--model", "gpt-5.6-terra",
                "-c", 'model_reasoning_effort="high"',
                "--dangerously-bypass-approvals-and-sandbox",
                "--cd", "/work/repository",
                "--output-schema", str(OUTCOME_SCHEMA_PATH),
                "--output-last-message", "/work/result.json",
                "$implement issue #7",
            ],
        )

    def test_claude_command_carries_the_profile_schema_without_uri_and_system_prompt(self):
        profile = AgentProfile("opus", "Opus", "medium", "claude")
        schema = json.loads(OUTCOME_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertIn("$schema", schema)
        del schema["$schema"]

        self.assertEqual(
            agent_command(profile, "/implement issue #7", REPOSITORY, OUTCOME_SCHEMA_PATH, OUTPUT),
            [
                "claude", "-p", "--model", "opus",
                "--effort", "medium",
                "--dangerously-skip-permissions",
                "--output-format", "json",
                "--json-schema", json.dumps(schema),
                "--append-system-prompt", SYSTEM_PROMPT,
                "/implement issue #7",
            ],
        )

    def test_implement_prefix_is_translated_for_claude_only(self):
        prompt = "$implement issue #7\n\nShared context"

        self.assertEqual(session_prompt("claude", prompt), "/implement issue #7\n\nShared context")
        self.assertEqual(session_prompt("codex", prompt), prompt)

    def test_prompt_without_implement_prefix_is_unchanged_for_claude(self):
        prompt = "Review the changes since the issue base commit."

        self.assertEqual(session_prompt("claude", prompt), prompt)


class ResultParsingTests(NoSubprocessTestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output_path = pathlib.Path(directory.name) / "result.json"

    def claude_outcome(self, stdout):
        write_agent_result("claude", stdout, self.output_path)
        return parse_agent_result(self.output_path)

    def changed_envelope(self, edit_in_place):
        envelope = json.loads(CLAUDE_STDOUT)
        edit_in_place(envelope)
        return json.dumps(envelope)

    def assert_claude_rejected(self, stdout, message):
        with self.assertRaisesRegex(ValueError, f"^{re.escape(message)}$"):
            write_agent_result("claude", stdout, self.output_path)
        self.assertFalse(self.output_path.exists())

    def test_recorded_claude_envelope_and_codex_result_give_the_same_outcome(self):
        claude = self.claude_outcome(CLAUDE_STDOUT)

        self.output_path.write_text(CODEX_RESULT, encoding="utf-8")
        write_agent_result("codex", "codex stdout is ignored", self.output_path)
        codex = parse_agent_result(self.output_path)

        self.assertEqual(
            claude,
            {"outcome": "no_changes", "summary": "Fixture session: nothing to do.", "findings": []},
        )
        self.assertEqual(codex, claude)

    def test_invalid_json_is_rejected(self):
        self.assert_claude_rejected(CLAUDE_STDOUT[:-10], "Claude Code did not return valid JSON output")

    def test_non_object_envelope_is_rejected(self):
        stdout = json.dumps([json.loads(CLAUDE_STDOUT)])

        self.assert_claude_rejected(stdout, "Claude Code output is not an object")

    def test_error_envelope_is_rejected(self):
        stdout = self.changed_envelope(lambda envelope: envelope.update(is_error=True))

        self.assert_claude_rejected(stdout, "Claude Code reported an error; inspect retained session output")

    def test_missing_structured_output_is_rejected(self):
        stdout = self.changed_envelope(lambda envelope: envelope.pop("structured_output"))

        self.assert_claude_rejected(stdout, "Claude Code did not return a structured_output object")

    def test_invalid_codex_result_file_is_rejected(self):
        self.output_path.write_text(CODEX_RESULT[:-10], encoding="utf-8")

        with self.assertRaisesRegex(MacatiaError, "^agent did not return valid structured output$"):
            parse_agent_result(self.output_path)


if __name__ == "__main__":
    unittest.main()
