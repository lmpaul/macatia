"""Offline readiness checks; no real agent or GitHub invocation."""

import contextlib
import io
import json
import pathlib
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock


KIT_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY = KIT_ROOT / "src"
FIXTURES = KIT_ROOT / "tests" / "fixtures"
sys.path.insert(0, str(SOURCE_DIRECTORY))

from macatia import check, cli


# Excerpts in the layout of the real help pages, so the tests need no engine installed.
CODEX_HELP = """Run Codex non-interactively

Usage: codex exec [OPTIONS] [PROMPT]

Options:
  -c, --config <key=value>
          Override a configuration value
  -m, --model <MODEL>
          Model the agent should use
      --dangerously-bypass-approvals-and-sandbox
          Skip all confirmation prompts
  -C, --cd <DIR>
          Tell the agent to use the specified directory as its working root
      --output-schema <FILE>
          Path to a JSON Schema file describing the model's final response shape
  -o, --output-last-message <FILE>
          Specifies file where the last message from the agent should be written
  -h, --help
          Print help
"""
CLAUDE_HELP = """Usage: claude [options] [command] [prompt]

Options:
  --append-system-prompt <prompt>       Append a system prompt to the default
                                        system prompt
  --dangerously-skip-permissions        Bypass all permission checks.
  --effort <level>                      Effort level for the current session
  --json-schema <schema>                JSON Schema for structured output
  --model <model>                       Model for the current session.
  --output-format <format>              Output format (only works with --print):
                                        "text", "json", or "stream-json"
  -p, --print                           Print response and exit (useful for
                                        pipes); also used with --verbose
  -h, --help                            Display help for command
"""



class TerminalOutput(io.StringIO):
    def isatty(self):
        return True

class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name).resolve()
        (self.root / "policy").mkdir()
        shutil.copy2(FIXTURES / "macatia.toml", self.root / "macatia.toml")
        shutil.copy2(FIXTURES / "quality.py", self.root / "policy/quality.py")
        self.checker = check
        self.config = self.root / "macatia.toml"
        text = self.config.read_text().replace("lmpaul/buyr", "example/project")
        text = text.replace('["lmpaul"]', '["reader"]')
        text = text.replace("gpt-5.6-terra", "chosen-model")
        text = text.replace(".github/scripts/quality.py", "policy/quality.py")
        text = text.replace(".github/quality-gates.json", "policy/quality-gates.json")
        self.config.write_text(text)
        self.policy = self.root / "policy/quality-gates.json"
        self.policy.write_text(json.dumps({
            "version": 1, "path_groups": {},
            "local_gates": [{"id": "tests", "paths": ["**"], "before_agent_review": True, "command": ["python3"]}],
            "ci": {"checks": {"project": {"name": "Project CI", "command": ["python3"]}}, "baseline": ["project"], "final": ["project"], "conditional": []},
        }))
        skill = self.root / ".agents/skills/implement"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("Implement")
        self.branch = "feature"
        self.dirty = ""
        self.diagnostics_ignored = True
        self.repo = "example/project"
        self.login = "reader"
        self.permission = True
        self.gh_version = "gh version 2.70.0 (2025-04-15)\nhttps://github.com/cli/cli/releases/tag/v2.70.0"
        self.labels = ["needs-triage", "needs-info", "ready-for-agent", "ready-for-human", "wontfix", "agent-working", "human-review", "implemented", "work-started", "standalone"]
        # GitHub lists colours as lowercase hex without #; each default label starts with its role's colour.
        self.colours = {role.replace("_", "-"): colour.lower() for role, colour in check.LABEL_COLOURS.items()}
        self.unrecolourable = set()
        self.commands = []
        self.help = {
            ("codex", "exec", "--help"): CODEX_HELP,
            ("claude", "--help"): CLAUDE_HELP,
        }

    def command(self, *args):
        self.commands.append(args)
        responses = {
            ("git", "rev-parse", "--show-toplevel"): str(self.root),
            ("git", "check-ref-format", "--branch", "main"): "",
            ("git", "remote", "get-url", "origin"): "git@github.com:example/project.git",
            ("git", "status", "--porcelain"): self.dirty,
            ("gh", "repo", "view", "--json", "nameWithOwner"): json.dumps({"nameWithOwner": self.repo}),
            ("gh", "api", "user"): json.dumps({"login": self.login}),
            ("gh", "--version"): self.gh_version,
            ("gh", "api", "repos/example/project"): json.dumps({"permissions": {"push": self.permission}}),
            ("gh", "api", "--paginate", "--slurp", "repos/example/project/labels"): json.dumps([[{"name": name, "color": self.colours.get(name, "ededed")} for name in self.labels]]),
        }
        if args[-1] == "--help":
            if isinstance(self.help[args], Exception):
                raise self.help[args]
            return self.help[args]
        if args == ("git", "check-ignore", "--quiet", ".macatia/"):
            if not self.diagnostics_ignored:
                raise RuntimeError("git check-ignore --quiet .macatia/ failed; check access and authentication")
            return ""
        if args == ("git", "symbolic-ref", "--quiet", "--short", "HEAD"):
            if self.branch is None:
                raise RuntimeError("git symbolic-ref --quiet --short HEAD failed")
            return self.branch
        if args[:3] == ("gh", "label", "create"):
            if args[3] == "broken":
                raise RuntimeError("gh label create failed")
            self.labels.append(args[3])
            self.colours[args[3]] = args[args.index("--color") + 1].lower()
            return ""
        if args[:3] == ("gh", "label", "edit"):
            if args[3] in self.unrecolourable:
                raise RuntimeError("gh label edit failed")
            self.colours[args[3]] = args[args.index("--color") + 1].lower()
            return ""
        # Only the explicit read-only allowlist, plus label creation and recolouring, is accepted by the fixture.
        return responses[args]

    def run_check(self, missing=None, confirm=None, with_review=False, terminal=False):
        output = TerminalOutput() if terminal else io.StringIO()
        with mock.patch.object(pathlib.Path, "cwd", return_value=self.root), mock.patch.object(self.checker.shutil, "which", side_effect=lambda name: None if name == missing else f"/tools/{name}"), contextlib.redirect_stdout(output):
            code = self.checker.run(self.root, self.config, with_review=with_review, confirm=confirm, command=self.command)
        return code, output.getvalue()

    def flags(self, local, ci):
        self.config.write_text(self.config.read_text().replace("local_checks = true", f"local_checks = {str(local).lower()}").replace("ci_checks = true", f"ci_checks = {str(ci).lower()}"))

    def test_a_terminal_sees_the_checks_in_progress(self):
        code, output = self.run_check(terminal=True)
        self.assertEqual(code, 0, output)
        self.assertIn("Checking that this repository is ready for Macatia", output)
        # The spinner is gone before the report.
        self.assertIn("No blockers", output.rsplit("Checking that this repository", 1)[1])

    def test_other_output_gets_no_progress(self):
        _, output = self.run_check()
        self.assertNotIn("Checking that this repository", output)

    def test_all_four_combinations(self):
        original = self.config.read_text()
        for local, ci in ((True, True), (True, False), (False, True), (False, False)):
            with self.subTest(local=local, ci=ci):
                self.config.write_text(original)
                self.flags(local, ci)
                self.assertEqual(self.run_check()[0], 0)

    def set_final_ci_check(self, value):
        text = self.config.read_text()
        self.config.write_text(text.replace("[verification]\n", f"[verification]\nfinal_ci_check = {str(value).lower()}\n"))

    def final_ci_check_rows(self, output):
        return " ".join(output.split()).count("! Final CI check")

    def test_final_ci_check_with_ci_checks_warns_that_it_has_no_effect(self):
        self.set_final_ci_check(True)
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        output = " ".join(output.split())  # The report wraps long details.
        self.assertIn("! Final CI check final_ci_check has no effect: ci_checks = true already checks CI after each child.", output)
        self.assertNotIn("local_checks = false", output)
        self.assertIn("No blockers", output)
        self.assertIn("1 warning", output)

    def test_final_ci_check_without_local_gates_recommends_them_without_failing(self):
        self.set_final_ci_check(True)
        self.flags(False, False)
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        output = " ".join(output.split())
        self.assertIn(
            "! Final CI check final_ci_check is on with local_checks = false: nothing verifies the children "
            "between them, only the final CI check verifies the run's final head. "
            "Set local_checks = true to run the local gates after each child.",
            output,
        )
        self.assertNotIn("has no effect", output)
        self.assertIn("1 check CI (not run)", output)
        self.assertIn("CI workflow check names confirmed", output)
        self.assertIn("No blockers", output)
        self.assertIn("1 warning", output)

    def test_final_ci_check_with_local_gates_and_without_ci_checks_gets_no_warning(self):
        self.set_final_ci_check(True)
        self.flags(True, False)
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.final_ci_check_rows(output), 0)
        self.assertIn("1 local check, 1 check CI (not run)", " ".join(output.split()))
        self.assertNotIn("warning", output)

    def test_final_ci_check_off_gets_no_warning(self):
        for local, ci in ((True, True), (False, False)):
            with self.subTest(local=local, ci=ci):
                self.setUp()
                self.set_final_ci_check(False)
                self.flags(local, ci)
                code, output = self.run_check()
                self.assertEqual(code, 0, output)
                self.assertEqual(self.final_ci_check_rows(output), 0)

    def test_an_active_final_ci_check_validates_the_ci_checks(self):
        self.set_final_ci_check(True)
        self.flags(True, False)
        self.policy.write_text(self.policy.read_text().replace("Project CI", "REPLACE_CHECK"))
        code, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn("CI checks still have placeholders", output)

    def test_disabled_policy_is_not_loaded(self):
        self.flags(False, False)
        self.policy.unlink()
        (self.root / "policy/quality.py").unlink()
        self.assertEqual(self.run_check()[0], 0)

    def test_disabled_local_placeholders_are_ignored(self):
        self.flags(False, True)
        self.policy.write_text(self.policy.read_text().replace('"python3"', '"REPLACE_COMMAND"'))
        # CI commands must still be configured when CI is active.
        manifest = json.loads(self.policy.read_text())
        manifest["ci"]["checks"]["project"]["command"] = ["python3"]
        self.policy.write_text(json.dumps(manifest))
        self.assertEqual(self.run_check()[0], 0)

    def test_disabled_ci_placeholders_are_ignored(self):
        self.flags(True, False)
        self.policy.write_text(self.policy.read_text().replace("Project CI", "REPLACE_CHECK"))
        self.assertEqual(self.run_check()[0], 0)

    def test_active_placeholders_block(self):
        self.policy.write_text(self.policy.read_text().replace('"python3"', '"REPLACE_COMMAND"'))
        self.assertEqual(self.run_check()[0], 1)

    def test_unconfigured_kit_stops_before_commands(self):
        self.config.write_text(self.config.read_text().replace("chosen-model", "CHOOSE_MODEL"))
        self.assertEqual(self.run_check()[0], 1)
        self.assertEqual(self.commands, [])

    def test_missing_agent_cli_blocks(self):
        self.assertEqual(self.run_check(missing="codex")[0], 1)

    def test_wrong_repository_blocks(self):
        self.repo = "other/repo"
        self.assertEqual(self.run_check()[0], 1)

    def test_base_branch_passes_with_the_run_branch_note(self):
        self.branch = "main"
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertIn("main (run will offer a macatia/issue-<parent> branch)", " ".join(output.split()))

    def test_other_branch_names_the_base(self):
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertIn("feature (base: main)", " ".join(output.split()))

    def test_detached_head_blocks(self):
        self.branch = None
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("detached HEAD, no active branch", " ".join(output.split()))

    def test_dirty_worktree_blocks(self):
        self.dirty = " M file"
        self.assertEqual(self.run_check()[0], 1)

    def test_ignored_diagnostics_directory_passes(self):
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertIn(".macatia/ ignored by Git", output)

    def test_diagnostics_directory_not_ignored_blocks_with_the_fix(self):
        self.diagnostics_ignored = False
        code, output = self.run_check()
        self.assertEqual(code, 1)
        output = " ".join(output.split())
        self.assertIn("Run diagnostics .macatia/ is not ignored by Git", output)
        self.assertIn('→ echo ".macatia/" >> .gitignore, then commit .gitignore', output)
        self.assertIn("1 blocker", output)

    def test_untrusted_account_blocks(self):
        self.login = "other"
        self.assertEqual(self.run_check()[0], 1)

    def test_missing_write_permission_blocks(self):
        self.permission = False
        self.assertEqual(self.run_check()[0], 1)

    def test_missing_label_blocks(self):
        self.labels.remove("ready-for-agent")
        self.assertEqual(self.run_check()[0], 1)

    def test_missing_labels_are_created_once_confirmed(self):
        self.labels.remove("implemented")
        questions = []
        code, output = self.run_check(confirm=lambda question: questions.append(question) or True)
        self.assertEqual(code, 0)
        self.assertEqual(questions, ["Create the 1 missing label on example/project?"])
        self.assertIn("✓ implemented created", output)
        self.assertIn(("gh", "label", "create", "implemented", "--repo", "example/project", "--description", "Implemented by Macatia", "--color", "C2E0C6"), self.commands)

    def test_missing_agent_working_label_is_reported_and_created_once_confirmed(self):
        self.labels.remove("agent-working")
        self.assertEqual(self.run_check()[0], 1)
        code, output = self.run_check(confirm=lambda question: True)
        self.assertEqual(code, 0)
        self.assertIn("✓ agent-working created", output)
        self.assertIn(
            ("gh", "label", "create", "agent-working", "--repo", "example/project",
             "--description", "A Macatia agent is working on this child", "--color", "5319E7"),
            self.commands,
        )

    def test_missing_work_started_label_is_reported_and_created_once_confirmed(self):
        self.labels.remove("work-started")
        self.assertEqual(self.run_check()[0], 1)
        code, output = self.run_check(confirm=lambda question: True)
        self.assertEqual(code, 0)
        self.assertIn("✓ work-started created", output)
        self.assertIn(
            ("gh", "label", "create", "work-started", "--repo", "example/project",
             "--description", "Macatia has started work on this parent", "--color", "E4E9F2"),
            self.commands,
        )

    def test_configuration_from_before_work_started_gets_the_default_label(self):
        text = self.config.read_text()
        self.config.write_text(text.replace('work_started = "work-started"\n', ""))
        self.labels.remove("work-started")
        code, output = self.run_check(confirm=lambda question: True)
        self.assertEqual(code, 0, output)
        self.assertIn("✓ work-started created", output)
        output = " ".join(output.split())  # The report wraps long details.
        self.assertIn("! Label roles macatia.toml leaves out work_started, so work-started is used", output)
        self.assertIn("→ macatia update-config writes it", output)

    def test_missing_standalone_marker_is_reported_and_created_once_confirmed(self):
        self.labels.remove("standalone")
        self.assertEqual(self.run_check()[0], 1)
        code, output = self.run_check(confirm=lambda question: True)
        self.assertEqual(code, 0)
        self.assertIn("✓ standalone created", output)
        self.assertIn(
            ("gh", "label", "create", "standalone", "--repo", "example/project",
             "--description", "Macatia has started work on this standalone issue", "--color", "E4E9F2"),
            self.commands,
        )

    def test_configuration_from_before_standalone_gets_the_default_label(self):
        text = self.config.read_text()
        self.config.write_text(text.replace('standalone = "standalone"\n', ""))
        code, output = self.run_check()
        output = " ".join(output.split())  # The report wraps long details.
        self.assertIn("! Label roles macatia.toml leaves out standalone, so standalone is used", output)

    def test_complete_labels_get_no_label_roles_warning(self):
        code, output = self.run_check()
        self.assertNotIn("Label roles", output)

    def declare_every_defaulted_setting(self):
        text = self.config.read_text().replace(
            "\n[labels]",
            'review_by_default = false\nreview_mode = "child"\ncaffeinate_by_default = false\n\n[labels]',
        )
        text = text.replace("ci_checks = true\n", "ci_checks = true\nfinal_ci_check = false\n")
        self.config.write_text(text + "\n[child_session_limit]\nstandard = 5\nreviewed = 9\n")

    def test_defaulted_settings_get_one_info_entry_without_failing_the_check(self):
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        output = " ".join(output.split())  # The report wraps long details.
        self.assertIn(
            "ℹ Defaulted settings macatia.toml leaves out caffeinate_by_default,"
            " child_session_limit.reviewed, child_session_limit.standard, review_by_default,"
            " review_mode, verification.final_ci_check, so their default values are used",
            output,
        )
        self.assertIn("→ macatia update-config writes them", output)
        self.assertEqual(output.count("Defaulted settings"), 1)
        self.assertIn("No blockers", output)

    def test_one_defaulted_setting_is_reported_alone(self):
        self.declare_every_defaulted_setting()
        self.config.write_text(self.config.read_text().replace("reviewed = 9\n", ""))
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        output = " ".join(output.split())
        self.assertIn(
            "ℹ Defaulted settings macatia.toml leaves out child_session_limit.reviewed,"
            " so its default value is used",
            output,
        )
        self.assertIn("→ macatia update-config writes it", output)

    def test_a_file_declaring_every_defaulted_setting_gets_no_entry(self):
        self.declare_every_defaulted_setting()
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertNotIn("Defaulted settings", output)
        self.assertNotIn("Label roles", output)

    def test_defaulted_label_roles_keep_their_warning_and_stay_out_of_the_info_entry(self):
        self.declare_every_defaulted_setting()
        self.config.write_text(self.config.read_text().replace('work_started = "work-started"\n', ""))
        code, output = self.run_check(confirm=lambda question: True)
        output = " ".join(output.split())
        self.assertIn("! Label roles macatia.toml leaves out work_started", output)
        self.assertNotIn("Defaulted settings", output)

    def test_optional_profiles_left_out_are_not_defaulted_settings(self):
        self.declare_every_defaulted_setting()
        text = self.config.read_text()
        self.assertNotIn("[models.backup]", text)
        self.config.write_text(re.sub(r"\[models\.review\]\n(?:[^\n\[]+\n)*", "", text))
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertNotIn("Defaulted settings", output)

    def test_missing_label_key_names_the_line_to_add_and_keeps_the_file(self):
        text = self.config.read_text().replace('agent_working = "agent-working"\n', "")
        self.config.write_text(text)
        code, output = self.run_check()
        output = " ".join(output.split())  # The report wraps long details.
        self.assertEqual(code, 1)
        self.assertIn('add agent_working = "agent-working" under [labels], or run macatia update-config', output)
        self.assertIn("edit macatia.toml by hand", output)
        self.assertNotIn("regenerate", output)
        self.assertEqual(self.config.read_text(), text)

    def test_labels_are_not_created_later_or_without_terminal(self):
        self.labels.remove("implemented")
        for confirm in (None, lambda question: False):
            with self.subTest(confirm=confirm):
                self.assertEqual(self.run_check(confirm=confirm)[0], 1)
                self.assertFalse(any(args[:3] == ("gh", "label", "create") for args in self.commands))

    def test_failed_label_creation_still_blocks(self):
        self.config.write_text(self.config.read_text().replace('implemented = "implemented"', 'implemented = "broken"'))
        code, output = self.run_check(confirm=lambda question: True)
        self.assertEqual(code, 1)
        self.assertIn("✗ broken not created", output)

    def label_edits(self):
        return [args for args in self.commands if args[:3] == ("gh", "label", "edit")]

    def test_a_label_with_the_wrong_colour_warns_without_blocking(self):
        self.colours["implemented"] = "ededed"
        self.colours["wontfix"] = "ffffff"
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        output = " ".join(output.split())
        self.assertIn("Labels all 10 present", output)
        self.assertIn("Label colours 2 with the wrong colour: implemented, wontfix", output)
        self.assertIn("No blockers", output)
        self.assertIn("1 warning", output)

    def test_without_a_terminal_the_wrong_colour_shows_the_hand_run_command_and_is_not_fixed(self):
        self.colours["implemented"] = "ededed"
        self.colours["wontfix"] = "ffffff"
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertIn(
            "gh label edit implemented --repo example/project --color C2E0C6; gh label edit wontfix --repo example/project --color CFD3D7",
            " ".join(output.split()),
        )
        self.assertEqual(self.label_edits(), [])

    def test_colours_differing_only_in_case_or_by_a_leading_hash_are_not_reported(self):
        self.colours["implemented"] = "C2E0C6"
        self.colours["wontfix"] = "#cfd3d7"
        code, output = self.run_check(confirm=lambda question: self.fail(question))
        self.assertEqual(code, 0, output)
        self.assertNotIn("Label colours", output)

    def test_a_renamed_role_is_compared_against_its_role_colour(self):
        self.config.write_text(self.config.read_text().replace('implemented = "implemented"', 'implemented = "done"'))
        self.labels[self.labels.index("implemented")] = "done"
        self.colours["done"] = "c2e0c6"
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertNotIn("Label colours", output)
        self.colours["done"] = "cfd3d7"
        code, output = self.run_check()
        self.assertIn("Label colours 1 with the wrong colour: done", " ".join(output.split()))
        self.assertIn("gh label edit done --repo example/project --color C2E0C6", " ".join(output.split()))

    def test_wrong_colours_are_recoloured_once_confirmed_after_the_missing_labels_question(self):
        self.labels.remove("human-review")
        self.colours["implemented"] = "ededed"
        self.colours["wontfix"] = "ffffff"
        questions = []
        code, output = self.run_check(confirm=lambda question: questions.append(question) or True)
        self.assertEqual(code, 0, output)
        self.assertEqual(questions, [
            "Create the 1 missing label on example/project?",
            "Recolour 2 labels with the wrong colour on example/project (implemented, wontfix)?",
        ])
        self.assertEqual(self.label_edits(), [
            ("gh", "label", "edit", "implemented", "--repo", "example/project", "--color", "C2E0C6"),
            ("gh", "label", "edit", "wontfix", "--repo", "example/project", "--color", "CFD3D7"),
        ])
        self.assertIn("✓ implemented recoloured", output)
        self.assertIn("✓ wontfix recoloured", output)

    def test_a_label_just_created_is_not_offered_for_recolouring(self):
        self.labels.remove("implemented")
        self.colours["wontfix"] = "ffffff"
        questions = []
        self.run_check(confirm=lambda question: questions.append(question) or True)
        self.assertEqual(questions[1], "Recolour 1 label with the wrong colour on example/project (wontfix)?")
        self.assertEqual([args[3] for args in self.label_edits()], ["wontfix"])

    def test_a_failed_recolouring_is_reported_by_name_and_the_others_are_recoloured(self):
        self.colours["implemented"] = "ededed"
        self.colours["wontfix"] = "ffffff"
        self.unrecolourable = {"implemented"}
        code, output = self.run_check(confirm=lambda question: True)
        self.assertEqual(code, 0, output)
        self.assertIn("✗ implemented not recoloured", output)
        self.assertIn("✓ wontfix recoloured", output)
        self.assertEqual(self.colours["wontfix"], "cfd3d7")

    def test_declining_the_recolouring_writes_nothing(self):
        self.colours["implemented"] = "ededed"
        code, output = self.run_check(confirm=lambda question: False)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.label_edits(), [])
        self.assertIn("Labels not recoloured; macatia check will offer again.", output)

    def test_broken_claude_skill_link_blocks(self):
        self.config.write_text(self.config.read_text().replace('engine = "codex"', 'engine = "claude"'))
        self.assertEqual(self.run_check()[0], 1)
        skill = self.root / ".claude/skills/implement"
        skill.parent.mkdir(parents=True)
        skill.symlink_to("../../.agents/skills/implement")
        self.assertEqual(self.run_check()[0], 0)
        (self.root / ".agents/skills/implement/SKILL.md").unlink()
        self.assertEqual(self.run_check()[0], 1)

    def use_engines(self, implementation, correction):
        text = self.config.read_text()
        for role, engine in (("implementation", implementation), ("correction", correction)):
            text = text.replace(f'[models.{role}]\nengine = "codex"', f'[models.{role}]\nengine = "{engine}"')
        self.config.write_text(text)
        (self.root / ".claude/skills").mkdir(parents=True, exist_ok=True)
        (self.root / ".claude/skills/implement").symlink_to("../../.agents/skills/implement")

    def help_requests(self):
        return [args for args in self.commands if args[-1] == "--help"]

    def test_only_the_configured_engines_have_their_flags_checked(self):
        for implementation, correction, expected in (
            ("codex", "codex", [("codex", "exec", "--help")]),
            ("claude", "claude", [("claude", "--help")]),
            ("codex", "claude", [("claude", "--help"), ("codex", "exec", "--help")]),
        ):
            with self.subTest(implementation=implementation, correction=correction):
                self.setUp()
                self.use_engines(implementation, correction)
                code, output = self.run_check()
                self.assertEqual(code, 0, output)
                self.assertEqual(self.help_requests(), expected)

    def use_review_profile(self, engine, name="opus"):
        # Claude engines also need the implement skill linked under .claude/skills.
        text = self.config.read_text().replace(
            '[models.review]\nengine = "codex"\nname = "gpt-5.6-sol"',
            f'[models.review]\nengine = "{engine}"\nname = "{name}"',
        )
        self.config.write_text(text)
        (self.root / ".claude/skills").mkdir(parents=True, exist_ok=True)
        (self.root / ".claude/skills/implement").symlink_to("../../.agents/skills/implement")

    def set_review_by_default(self, value):
        self.config.write_text(f"review_by_default = {value}\n" + self.config.read_text())

    def test_review_role_is_checked_with_review_or_review_by_default(self):
        for setting, with_review, checked in (
            (None, False, False),
            ("false", False, False),
            ("true", False, True),
            (None, True, True),
            ("false", True, True),
            ("true", True, True),
        ):
            with self.subTest(setting=setting, with_review=with_review):
                self.setUp()
                self.use_review_profile("claude")
                if setting is not None:
                    self.set_review_by_default(setting)
                code, output = self.run_check(with_review=with_review)
                self.assertEqual(code, 0, output)
                expected = [("claude", "--help"), ("codex", "exec", "--help")] if checked else [("codex", "exec", "--help")]
                self.assertEqual(self.help_requests(), expected)

    def test_review_by_default_checks_the_review_model_profile(self):
        self.use_review_profile("codex", "CHOOSE_MODEL")
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.commands = []
        self.set_review_by_default("true")
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("still have placeholders", output)
        self.assertEqual(self.commands, [])

    def test_review_by_default_without_review_profile_is_reported(self):
        text = self.config.read_text()
        start = text.index("[models.review]")
        self.config.write_text(text[:start] + text[text.index("[models.correction]"):])
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.commands = []
        self.set_review_by_default("true")
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("configuration.models is missing keys: review", output)
        self.assertEqual(self.commands, [])

    def test_review_role_is_checked_in_final_review_as_in_child_review(self):
        for mode in ("child", "final"):
            for setting, with_review, checked in (
                (None, False, False),
                ("true", False, True),
                (None, True, True),
            ):
                with self.subTest(mode=mode, setting=setting, with_review=with_review):
                    self.setUp()
                    self.use_review_profile("claude")
                    self.config.write_text(f'review_mode = "{mode}"\n' + self.config.read_text())
                    if setting is not None:
                        self.set_review_by_default(setting)
                    code, output = self.run_check(with_review=with_review)
                    self.assertEqual(code, 0, output)
                    expected = [("claude", "--help"), ("codex", "exec", "--help")] if checked else [("codex", "exec", "--help")]
                    self.assertEqual(self.help_requests(), expected)

    def test_final_review_by_default_without_review_profile_is_reported(self):
        text = self.config.read_text()
        start = text.index("[models.review]")
        self.config.write_text('review_mode = "final"\n' + text[:start] + text[text.index("[models.correction]"):])
        self.set_review_by_default("true")
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("configuration.models is missing keys: review", output)

    def test_invalid_review_mode_is_reported(self):
        self.config.write_text('review_mode = "both"\n' + self.config.read_text())
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn('configuration.review_mode must be "child" or "final"', output)

    def test_check_has_no_no_review_flag(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(["check", "--no-review"])
        self.assertEqual(raised.exception.code, 2)

    def set_worktree_setup(self, value):
        self.config.write_text(f"worktree_setup = {value}\n" + self.config.read_text())

    def test_a_worktree_setup_command_is_accepted_and_never_run(self):
        self.set_worktree_setup('"touch ran-worktree-setup"')
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertFalse(any("touch ran-worktree-setup" in " ".join(args) for args in self.commands))
        self.assertFalse((self.root / "ran-worktree-setup").exists())

    def test_a_worktree_setup_that_is_not_a_non_empty_string_fails_the_configuration(self):
        for value in ('""', '"   "', "true", "1", '["uv", "sync"]'):
            with self.subTest(value=value):
                self.setUp()
                self.set_worktree_setup(value)
                code, output = self.run_check()
                self.assertEqual(code, 1, output)
                self.assertIn("configuration.worktree_setup must be a non-empty string", output)
                self.assertEqual(self.commands, [])

    def set_caffeinate_by_default(self, value):
        self.config.write_text(f"caffeinate_by_default = {value}\n" + self.config.read_text())

    def sleep_lock_row(self, output):
        return [line for line in output.splitlines() if "Sleep lock" in line]

    def test_caffeinate_by_default_warns_when_the_sleep_lock_helper_is_missing(self):
        self.set_caffeinate_by_default("true")
        with mock.patch.object(sys, "platform", "linux"):
            code, output = self.run_check(missing="systemd-inhibit")
        self.assertEqual(code, 0, output)
        self.assertIn("systemd-inhibit not found", output)
        self.assertIn("the machine may sleep", output)
        self.assertIn("1 warning", output)
        self.assertIn("No blockers", output)

    def test_caffeinate_by_default_warns_on_a_platform_without_a_sleep_lock(self):
        self.set_caffeinate_by_default("true")
        with mock.patch.object(sys, "platform", "freebsd14"):
            code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertIn("not supported on freebsd14", output)
        self.assertIn("1 warning", output)

    def test_caffeinate_by_default_with_the_helper_available_shows_the_sleep_lock_ok(self):
        self.set_caffeinate_by_default("true")
        with mock.patch.object(sys, "platform", "darwin"):
            code, output = self.run_check()
        self.assertEqual(code, 0, output)
        row = self.sleep_lock_row(output)
        self.assertEqual(len(row), 1, output)
        self.assertIn("✓", row[0])
        self.assertIn("caffeinate", row[0])
        self.assertNotIn("warning", output)

    def test_no_sleep_lock_row_without_caffeinate_by_default(self):
        for setting in (None, "false"):
            with self.subTest(setting=setting):
                self.setUp()
                if setting is not None:
                    self.set_caffeinate_by_default(setting)
                with mock.patch.object(sys, "platform", "freebsd14"):
                    code, output = self.run_check()
                self.assertEqual(code, 0, output)
                self.assertEqual(self.sleep_lock_row(output), [])

    def test_sleep_lock_warning_does_not_hide_blockers(self):
        self.set_caffeinate_by_default("true")
        self.dirty = " M file.py"
        with mock.patch.object(sys, "platform", "freebsd14"):
            code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("1 blocker", output)
        self.assertIn("1 warning", output)

    def test_check_has_no_sleep_lock_flag(self):
        for flag in ("--caffeinate", "--no-caffeinate"):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                cli.main(["check", flag])
            self.assertEqual(raised.exception.code, 2)

    def add_backup(self, engine, name):
        text = self.config.read_text().replace("\n[verification]", f'\n[models.backup]\nengine = "{engine}"\nname = "{name}"\ndisplay_name = "Backup"\nreasoning_effort = "medium"\n\n[verification]')
        self.config.write_text(text)
        if engine == "claude":
            (self.root / ".claude/skills").mkdir(parents=True, exist_ok=True)
            (self.root / ".claude/skills/implement").symlink_to("../../.agents/skills/implement")

    def test_backup_engine_has_its_cli_and_flags_checked(self):
        self.add_backup("claude", "opus")
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.help_requests(), [("claude", "--help"), ("codex", "exec", "--help")])
        self.assertIn("Flags (claude)", output)

    def test_backup_on_the_regular_engine_adds_no_check(self):
        self.add_backup("codex", "other-model")
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.help_requests(), [("codex", "exec", "--help")])

    def test_missing_backup_cli_blocks(self):
        self.add_backup("claude", "opus")
        code, output = self.run_check(missing="claude")
        self.assertEqual(code, 1)
        self.assertIn("claude not found in PATH", output)
        self.assertIn("not verified: claude missing", output)

    def test_flag_missing_from_backup_engine_help_blocks(self):
        self.add_backup("claude", "opus")
        self.help[("claude", "--help")] = CLAUDE_HELP.replace("--json-schema", "--json")
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("Claude Code does not list --json-schema in claude --help", " ".join(output.split()))
        self.assertIn("1 blocker", output)

    def test_backup_model_placeholder_blocks_before_commands(self):
        self.add_backup("claude", "CHOOSE_MODEL")
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("still have placeholders", output)
        self.assertEqual(self.commands, [])

    def test_flags_missing_from_help_block_with_engine_flags_and_fix(self):
        wrapped = "  --bare                                Minimal mode; still honours\n                                        --json-schema, --add-dir\n"
        for engine, page, expected in (
            ("codex", CODEX_HELP.replace("--output-schema", "--output-format"), "Codex does not list --output-schema in codex exec --help"),
            ("codex", CODEX_HELP.replace("--cd <DIR>", "--dir <DIR>").replace("-o, --output-last-message", "-o, --last-message"), "Codex does not list --cd, --output-last-message in codex exec --help"),
            ("claude", CLAUDE_HELP.replace("--json-schema", "--json"), "Claude Code does not list --json-schema in claude --help"),
            # Flags only mentioned in descriptions, on the option's own line or a wrapped line, are not listed.
            ("claude", CLAUDE_HELP.replace("  -p, --print     ", "  --no-print      "), "Claude Code does not list -p in claude --help"),
            ("claude", CLAUDE_HELP.replace("--json-schema", "--json") + wrapped, "Claude Code does not list --json-schema in claude --help"),
        ):
            with self.subTest(expected=expected):
                self.setUp()
                self.use_engines(engine, engine)
                help_command = ("codex", "exec", "--help") if engine == "codex" else ("claude", "--help")
                self.help[help_command] = page
                code, output = self.run_check()
                self.assertEqual(code, 1)
                output = " ".join(output.split())
                self.assertIn(expected, output)
                self.assertIn(f"→ update {check.AGENT_CLIS[engine]}, or check that macatia supports the installed version", output)

    def test_variadic_option_is_listed(self):
        self.help[("codex", "exec", "--help")] = CODEX_HELP.replace("  -C, --cd <DIR>", "  -C, --cd <DIR>...")
        code, output = self.run_check()
        self.assertEqual(code, 0, output)

    def test_failing_or_missing_help_command_is_a_readiness_error(self):
        for error in (RuntimeError("codex exec --help failed; check access and authentication"), FileNotFoundError(2, "No such file or directory", "codex")):
            with self.subTest(error=error):
                self.help[("codex", "exec", "--help")] = error
                code, output = self.run_check()
                self.assertEqual(code, 1)
                self.assertIn("Flags (codex)", output)
                self.assertIn("→ reinstall Codex", output)
                self.assertIn("1 blocker", output)

    def test_missing_engine_skips_its_flag_check(self):
        code, output = self.run_check(missing="codex")
        self.assertEqual(code, 1)
        self.assertEqual(self.help_requests(), [])
        self.assertIn("not verified: codex missing", output)

    def test_missing_config_points_to_init(self):
        self.config.unlink()
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("→ macatia init", output)
        self.assertEqual(self.commands, [])

    def test_run_without_config_points_to_init(self):
        import subprocess

        self.config.unlink()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        for review in ([], ["--review"]):
            with self.subTest(review=review):
                stderr = io.StringIO()
                with (
                    contextlib.chdir(self.root),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(stderr),
                    # The CLI turns bytecode writing off for the whole process.
                    mock.patch.object(sys, "dont_write_bytecode", sys.dont_write_bytecode),
                ):
                    code = cli.main(["run", *review, "1"])
                self.assertEqual(code, 1)
                self.assertIn("macatia.toml not found: run `macatia init` first", stderr.getvalue())

    def test_leftover_ralph_toml_asks_for_rename(self):
        self.config.rename(self.root / "ralph.toml")
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("→ git mv ralph.toml macatia.toml", output)
        self.assertEqual(self.commands, [])

    def test_run_with_leftover_ralph_toml_asks_for_rename(self):
        import subprocess

        self.config.rename(self.root / "ralph.toml")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        stderr = io.StringIO()
        with (
            contextlib.chdir(self.root),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
            mock.patch.object(sys, "dont_write_bytecode", sys.dont_write_bytecode),
        ):
            code = cli.main(["run", "1"])
        self.assertEqual(code, 1)
        self.assertIn("macatia.toml not found, but ralph.toml is: rename it with `git mv ralph.toml macatia.toml`", stderr.getvalue())

    def test_missing_git_skips_git_checks(self):
        code, output = self.run_check(missing="git")
        self.assertEqual(code, 1)
        self.assertFalse([args for args in self.commands if args[0] == "git"])
        self.assertEqual(output.count("not verified: git missing"), 5)

    def test_missing_gh_skips_github_checks(self):
        code, output = self.run_check(missing="gh")
        self.assertEqual(code, 1)
        self.assertFalse([args for args in self.commands if args[0] == "gh"])
        self.assertIn("install GitHub CLI", output)
        self.assertEqual(output.count("not verified: gh missing"), 5)
        self.assertIn("1 blocker", output)

    def test_gh_older_than_the_minimum_version_blocks_and_skips_github_checks(self):
        self.gh_version = "gh version 2.67.0 (2025-02-11)"
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("gh 2.67.0, 2.70.0 or newer required", output)
        self.assertIn("update GitHub CLI to 2.70.0 or newer", output)
        self.assertEqual(output.count("not verified: gh missing"), 5)

    def test_gh_version_is_shown_when_it_is_recent_enough(self):
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertIn("2.70.0 (/tools/gh)", output)

    def test_failures_show_the_fix(self):
        self.labels.remove("implemented")
        self.labels.remove("wontfix")
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("2 missing: implemented, wontfix", output)
        self.assertIn("gh label create implemented --color C2E0C6; gh label create wontfix --color CFD3D7", " ".join(output.split()))

    def test_command_failure_does_not_echo_cli_output(self):
        import subprocess

        result = subprocess.CompletedProcess(["gh"], 1, "sensitive token", "sensitive token")
        with (
            mock.patch.object(self.checker.subprocess, "run", return_value=result),
            self.assertRaises(RuntimeError) as error,
        ):
            self.checker.Readiness(self.root).command("gh", "api", "user")
        self.assertNotIn("sensitive", str(error.exception))


class ArrowSelectTests(unittest.TestCase):
    def select(self, keys, options, default):
        """The arrow selector on a scripted keyboard; returns the answer and what the screen showed."""
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output.plain_text import PlainTextOutput

        screen = io.StringIO()
        with create_pipe_input() as keyboard, create_app_session(input=keyboard, output=PlainTextOutput(screen)):
            keyboard.send_text(keys)
            return check.arrow_select("Pick", options, default), screen.getvalue()

    def test_unavailable_options_are_shown_with_their_reason_and_skipped(self):
        down, enter = "\x1b[B", "\r"
        options = [("low", ""), ("medium", "", "same as implementation"), ("high", "")]
        answer, screen = self.select(down + enter, options, "low")
        # Moving down from low skips medium.
        self.assertEqual(answer, "high")
        self.assertIn("medium", screen)
        self.assertIn("same as implementation", screen)


if __name__ == "__main__":
    unittest.main()
