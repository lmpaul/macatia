"""`ralph-kit init` without real git, gh, or terminal."""

import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


KIT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT / "src"))

from ralph_kit import check, init
from ralph_kit.config import load_config


DEFAULT_CONFIGURATION = """\
# Ralph configuration for this repository. See https://github.com/lmpaul/ralph-kit
version = 1
repository = "acme/widget"
base_branch = "trunk"
remote = "origin"
trusted_authors = ["octocat"]
# true makes every run a reviewed run; ralph-kit run --no-review overrides it.
review_by_default = false
# true makes every run hold a sleep lock; ralph-kit run --no-caffeinate overrides it.
caffeinate_by_default = false

[labels]
needs_triage = "needs-triage"
needs_info = "needs-info"
ready_for_agent = "ready-for-agent"
ready_for_human = "ready-for-human"
wontfix = "wontfix"
agent_working = "agent-working"
human_review = "human-review"
implemented = "implemented"

[models.implementation]
engine = "claude"
name = "sonnet"
display_name = "sonnet"
reasoning_effort = "medium"

# Used by reviewed runs: ralph-kit run --review, or every run when review_by_default = true.
[models.review]
engine = "claude"
name = "opus"
display_name = "opus"
reasoning_effort = "high"

[models.correction]
engine = "claude"
name = "sonnet"
display_name = "sonnet"
reasoning_effort = "medium"

# Takes over every session for the rest of a run after an engine failure.
[models.backup]
engine = "codex"
name = "codex-large"
display_name = "codex-large"
reasoning_effort = "medium"

[verification]
# Both checks need the quality module and manifest below; disabled checks do not load them.
local_checks = false
ci_checks = false
quality_module = ".github/scripts/quality.py"
quality_manifest = ".github/quality-gates.json"
"""
# Values init never asks about, as the renderer writes them.
CUSTOM_CONFIGURATION = """\
# Ralph configuration for this repository. See https://github.com/lmpaul/ralph-kit
version = 1
repository = "acme/gadget"
base_branch = "develop"
remote = "upstream"
trusted_authors = ["alice", "bob", "octocat"]
# true makes every run a reviewed run; ralph-kit run --no-review overrides it.
review_by_default = false
# true makes every run hold a sleep lock; ralph-kit run --no-caffeinate overrides it.
caffeinate_by_default = false

[labels]
needs_triage = "triage: new"
needs_info = "triage: info"
ready_for_agent = "agent: ready"
ready_for_human = "human: ready"
wontfix = "won't fix"
agent_working = "agent: working"
human_review = "human: review"
implemented = "done"

[models.implementation]
engine = "claude"
name = "sonnet"
display_name = "Sonnet \\"daily\\""
reasoning_effort = "medium"

# Used by reviewed runs: ralph-kit run --review, or every run when review_by_default = true.
[models.review]
engine = "codex"
name = "gpt-5"
display_name = "GPT-5 review"
reasoning_effort = "xhigh"

[models.correction]
engine = "claude"
name = "opus"
display_name = "Opus fixer"
reasoning_effort = "high"

# Takes over every session for the rest of a run after an engine failure.
[models.backup]
engine = "codex"
name = "gpt-5-mini"
display_name = "Mini"
reasoning_effort = "low"

[verification]
# Both checks need the quality module and manifest below; disabled checks do not load them.
local_checks = true
ci_checks = false
quality_module = "tools/policy/quality.py"
quality_manifest = "tools/policy/gates.json"
"""


class InitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, env=self.git_environment())
        self.responses = {
            ("git", "rev-parse", "--show-toplevel"): str(self.root),
            ("gh", "auth", "status"): "",
            ("gh", "--version"): "gh version 2.102.0 (2026-09-30)",
            ("gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef"): json.dumps(
                {"nameWithOwner": "acme/widget", "defaultBranchRef": {"name": "trunk"}}
            ),
            ("git", "remote"): "origin",
            ("gh", "api", "user", "--jq", ".login"): "octocat",
            ("codex", "debug", "models"): json.dumps({"models": [
                {"slug": "codex-hidden", "visibility": "hide", "priority": 1, "supported_reasoning_levels": []},
                {"slug": "codex-small", "description": "Fast", "visibility": "list", "priority": 9,
                 "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}]},
                {"slug": "codex-large", "description": "Smart", "visibility": "list", "priority": 2,
                 "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "high"}, {"effort": "ultra"}]},
            ]}),
        }
        self.installed = {"claude", "codex"}
        self.tools = {"git", "gh"}
        self.output = io.StringIO()

    @staticmethod
    def git_environment():
        """Real Git for ignore rules, without the developer's own global excludes."""
        return os.environ | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def command(self, *args):
        if args[:2] == ("git", "check-ignore"):
            completed = subprocess.run(args, cwd=self.root, capture_output=True, text=True, env=self.git_environment())
            return completed.stdout.strip() if completed.returncode == 0 else None
        return self.responses.get(args)

    def run_init(self, answers=None, force=False, config=None):
        """answers=None simulates a non-interactive stdin; otherwise one answer per prompt."""
        pending = list(answers or [])
        self.prompts = []

        def read(prompt):
            self.prompts.append(prompt)
            if not pending:
                raise EOFError
            return pending.pop(0)

        prompter = init.Prompter(interactive=answers is not None, read=read, output=self.output)
        options = {} if config is None else {"config": config}
        path = init.run(self.root, prompter, force=force, command=self.command, which=lambda name: name if name in self.installed | self.tools else None, **options)
        return load_config(path, self.root)

    def gitignore(self):
        path = self.root / ".gitignore"
        return path.read_bytes().decode() if path.exists() else None

    def test_detected_values_are_not_asked(self):
        # Only the engine, two models, two efforts and the backup are asked; Enter keeps every default.
        config = self.run_init(answers=[""] * 8)
        self.assertEqual(len(self.prompts), 8)
        self.assertEqual(config.repository, "acme/widget")
        self.assertEqual(config.base_branch, "trunk")
        self.assertEqual(config.remote, "origin")
        self.assertEqual(config.trusted_authors, {"octocat"})
        self.assertEqual(config.models["implementation"].engine, "claude")
        self.assertEqual(config.models["implementation"].name, "sonnet")
        self.assertEqual(config.models["implementation"].reasoning_effort, "medium")
        self.assertEqual(config.models["review"].name, "opus")
        self.assertEqual(config.models["review"].reasoning_effort, "high")
        self.assertEqual(config.models["correction"], config.models["implementation"])
        self.assertFalse(config.verification.local_checks or config.verification.ci_checks)
        self.assertFalse(config.review_by_default)
        self.assertFalse(config.caffeinate_by_default)

    def test_review_model_question_is_for_every_reviewed_run(self):
        self.run_init(answers=[""] * 8)
        output = self.output.getvalue()
        self.assertIn("? Model for the independent review (reviewed runs)", output)
        self.assertNotIn("run --review", output)

    def test_undetected_values_are_asked_and_validated(self):
        del self.responses[("gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef")]
        del self.responses[("gh", "api", "user", "--jq", ".login")]
        self.responses[("git", "remote")] = "origin\nupstream"
        self.installed = {"codex"}
        config = self.run_init(answers=[
            "widget", "acme/widget",      # repository: invalid, then valid
            "",                           # base branch: default main
            "2",                          # remote: upstream
            "alice, bob, alice",          # trusted authors, deduplicated
            "", "high",                   # implementation: first listed model, then effort
            "3", "custom-review", "",     # review: "other", free name, default effort
            "",                           # backup: default "No backup"
        ])
        self.assertEqual(config.repository, "acme/widget")
        self.assertIn("expected format", self.output.getvalue())
        self.assertEqual(config.base_branch, "main")
        self.assertEqual(config.remote, "upstream")
        self.assertEqual(config.trusted_authors, {"alice", "bob"})
        self.assertEqual(config.models["implementation"].engine, "codex")
        self.assertEqual(config.models["implementation"].name, "codex-large")
        self.assertEqual(config.models["implementation"].reasoning_effort, "high")
        self.assertEqual(config.models["review"].name, "custom-review")

    def test_only_installed_engines_are_offered(self):
        self.installed = {"claude"}
        self.run_init(answers=[""] * 5)
        self.assertIn("Claude Code (only CLI installed)", self.output.getvalue())
        self.assertNotIn("codex", self.output.getvalue())

    def test_no_agent_cli_stops_before_writing(self):
        self.installed = set()
        with self.assertRaisesRegex(init.InitError, "no agent CLI"):
            self.run_init(answers=[])
        self.assertFalse((self.root / "ralph.toml").exists())

    def test_choices_use_the_arrow_selector_when_available(self):
        asked = []

        def select(question, options, default):
            asked.append((question, [value for value, _ in options], default))
            return options[-1][0] if question.startswith("Reasoning effort") else default

        prompter = init.Prompter(interactive=True, read=lambda prompt: "", output=self.output, select=select)
        path = init.run(self.root, prompter, command=self.command, which=lambda name: name if name in self.installed | self.tools else None)
        config = load_config(path, self.root)
        self.assertEqual(asked[0], ("Agent engine", ["claude", "codex"], "claude"))
        self.assertEqual(asked[1][1], ["fable", "opus", "sonnet", "haiku", init.OTHER])
        self.assertEqual(len(asked), 8)
        self.assertEqual(config.models["implementation"].reasoning_effort, "max")
        self.assertNotIn("Choice [", self.output.getvalue())

    def select_backup(self, engine):
        """Run init with an arrow selector choosing the regular engine and every other default."""
        asked = []

        def select(question, options, default):
            asked.append((question, options, default))
            return engine if question == "Agent engine" else default

        prompter = init.Prompter(interactive=True, read=lambda prompt: "", output=self.output, select=select)
        path = init.run(self.root, prompter, command=self.command, which=lambda name: name if name in self.installed | self.tools else None)
        return load_config(path, self.root), asked

    def test_backup_recommends_the_other_installed_engine(self):
        for engine, other, model in (("claude", "codex", "codex-large"), ("codex", "claude", "opus")):
            with self.subTest(engine=engine):
                config, asked = self.select_backup(engine)
                questions = [question for question, _, _ in asked]
                # Asked after the regular model profiles, then the backup's own model and effort.
                self.assertTrue(questions[5].startswith("Backup"))
                _, options, default = asked[5]
                self.assertEqual([value for value, _ in options], [other, engine, init.NO_BACKUP])
                self.assertIn("recommended", options[0][1])
                self.assertNotIn("recommended", options[1][1])
                self.assertEqual(default, other)
                self.assertEqual(questions[6], "Model for the backup")
                self.assertEqual(questions[7], f"Reasoning effort for {model}")
                self.assertEqual(
                    (config.backup.engine, config.backup.name, config.backup.reasoning_effort),
                    (other, model, "medium"),
                )
                self.assertIn("[models.backup]", (self.root / "ralph.toml").read_text())
                (self.root / "ralph.toml").unlink()

    def test_single_engine_defaults_to_no_backup(self):
        self.installed = {"claude"}
        config = self.run_init(answers=[""] * 5)
        self.assertIsNone(config.backup)
        self.assertNotIn("[models.backup]", (self.root / "ralph.toml").read_text())
        output = self.output.getvalue()
        self.assertIn("? Backup", output)
        backup_options = output.split("? Backup", 1)[1]
        self.assertIn("1) claude", backup_options)
        self.assertIn(f"2) {init.NO_BACKUP}", backup_options)
        self.assertNotIn("codex", backup_options)
        self.assertEqual(self.prompts[-1], "Choice [2]: ")

    def test_clashing_backup_efforts_are_unavailable(self):
        self.installed = {"claude"}
        config = self.run_init(answers=[
            "", "", "", "",  # implementation sonnet/medium, review opus/high
            "1",             # backup: claude
            "3",             # sonnet stays available: only medium clashes
            "2",             # medium: refused
            "",              # default effort, the middle one now that medium is taken
        ])
        output = self.output.getvalue()
        efforts = output.split("? Reasoning effort for sonnet")[-1]
        self.assertIn("2) medium  (unavailable: same as implementation)", efforts)
        self.assertIn("3) high\n", efforts)
        self.assertIn("medium is unavailable: same as implementation.", efforts)
        self.assertNotIn("must differ", output)
        self.assertEqual(self.prompts[-2:], ["Choice [3]: "] * 2)  # Default moved off medium, asked again after refusal.
        self.assertEqual(
            (config.backup.engine, config.backup.name, config.backup.reasoning_effort),
            ("claude", "sonnet", "high"),
        )
        # The backup's engine is asked once.
        self.assertEqual(output.count("? Backup"), 1)

    def test_a_model_is_unavailable_when_every_effort_clashes(self):
        self.installed = {"codex"}
        config = self.run_init(answers=[
            "2", "1",           # implementation codex-small/low
            "2", "2",           # review codex-small/medium
            "1",                # backup: codex
            "2",                # codex-small: refused
            "3", "codex-small", # free name: clash explained, asked again
            "", "",             # codex-large, default effort
        ])
        output = self.output.getvalue()
        models = output.split("? Model for the backup")[1]
        self.assertIn("1) codex-large  (Smart)\n", models)
        self.assertIn(
            "2) codex-small  (Fast)  (unavailable: every reasoning effort same as implementation or review)", models
        )
        self.assertIn("codex-small is unavailable: every reasoning effort same as implementation or review.", models)
        self.assertEqual(output.count("? Model for the backup"), 2)
        self.assertEqual(output.count("? Backup"), 1)
        self.assertEqual(
            (config.backup.engine, config.backup.name, config.backup.reasoning_effort),
            ("codex", "codex-large", "medium"),
        )

    def test_the_arrow_selector_receives_unavailable_choices_with_their_reason(self):
        self.installed = {"claude"}
        asked = {}

        def select(question, options, default):
            asked[question] = (options, default)
            if question.startswith("Backup"):
                return "claude"
            return "sonnet" if question == "Model for the backup" else default

        prompter = init.Prompter(interactive=True, read=lambda prompt: "", output=self.output, select=select)
        path = init.run(self.root, prompter, command=self.command, which=lambda name: name if name in self.installed | self.tools else None)
        options, default = asked["Reasoning effort for sonnet"]
        self.assertIn(("medium", "", "same as implementation"), options)
        self.assertEqual(default, "high")
        self.assertEqual(load_config(path, self.root).backup.reasoning_effort, "high")

    def test_default_answers_write_the_default_configuration(self):
        self.run_init(answers=[""] * 8)
        self.assertEqual((self.root / "ralph.toml").read_text(), DEFAULT_CONFIGURATION)

    def test_no_backup_with_both_engines_writes_no_section(self):
        config = self.run_init(answers=[""] * 5 + ["3"])
        self.assertIsNone(config.backup)
        self.assertNotIn("[models.backup]", (self.root / "ralph.toml").read_text())

    def test_missing_labels_are_offered_and_created_after_writing(self):
        self.responses[("gh", "api", "--paginate", "--slurp", "repos/acme/widget/labels")] = json.dumps([[{"name": "needs-triage"}]])
        created = []

        def command(*args):
            if args[:3] == ("gh", "label", "create"):
                self.assertTrue((self.root / "ralph.toml").exists())
                created.append(args[3])
                return ""
            return self.responses.get(args)

        self.command = command
        self.run_init(answers=[""] * 9)  # Enter keeps "Yes".
        self.assertEqual(self.prompts[-1], "Choice [1]: ")
        self.assertEqual(created, [label for label in init.LABELS.values() if label != "needs-triage"])
        self.assertIn("✓ Label implemented created", self.output.getvalue())
        self.assertIn("✓ Label agent-working created", self.output.getvalue())

    def test_configuration_declares_the_agent_working_label(self):
        config = self.run_init()
        self.assertEqual(config.labels["agent_working"], "agent-working")
        self.assertIn('agent_working = "agent-working"\n', (self.root / "ralph.toml").read_text())

    def test_labels_are_kept_for_later(self):
        self.responses[("gh", "api", "--paginate", "--slurp", "repos/acme/widget/labels")] = json.dumps([[]])
        self.run_init(answers=[""] * 8 + ["2"])
        self.assertNotIn("created", self.output.getvalue())
        # Without a terminal, missing labels are only reported.
        self.run_init(force=True)
        self.assertIn("Labels missing on acme/widget", self.output.getvalue())
        self.assertNotIn("created", self.output.getvalue())

    def test_missing_tools_stop_before_any_probe(self):
        self.tools = {"git"}
        self.installed = set()
        probes = []
        self.command = lambda *args: probes.append(args)
        with self.assertRaises(init.InitError) as raised:
            self.run_init(answers=[])
        # Every missing prerequisite is reported at once, and nothing else runs.
        self.assertIn("gh not found in PATH", str(raised.exception))
        self.assertIn("no agent CLI", str(raised.exception))
        self.assertEqual(probes, [])
        self.assertFalse((self.root / "ralph.toml").exists())

    def test_gh_older_than_the_minimum_version_stops_before_questions(self):
        self.responses[("gh", "--version")] = "gh version 2.69.0 (2025-03-19)"
        with self.assertRaisesRegex(init.InitError, "gh 2.69.0, 2.70.0 or newer required"):
            self.run_init(answers=[])
        self.assertEqual(self.prompts, [])
        self.assertFalse((self.root / "ralph.toml").exists())

    def test_unauthenticated_gh_stops_before_questions(self):
        del self.responses[("gh", "auth", "status")]
        with self.assertRaisesRegex(init.InitError, "gh auth login"):
            self.run_init(answers=[])
        self.assertEqual(self.prompts, [])

    def test_codex_catalog_drives_models_and_efforts(self):
        self.installed = {"codex"}
        config = self.run_init(answers=["", "", "2", "", ""])
        output = self.output.getvalue()
        self.assertNotIn("codex-hidden", output)
        self.assertLess(output.index("codex-large"), output.index("codex-small"))
        self.assertEqual(config.models["implementation"].name, "codex-large")
        self.assertEqual(config.models["implementation"].reasoning_effort, "medium")
        self.assertEqual(config.models["review"].name, "codex-small")
        # codex-small only supports low and medium, so the "high" review default falls back.
        self.assertEqual(config.models["review"].reasoning_effort, "medium")
        self.assertNotIn("ultra", output.split("codex-small", 2)[-1])

    def test_unavailable_codex_catalog_falls_back_to_free_text(self):
        self.installed = {"codex"}
        del self.responses[("codex", "debug", "models")]
        config = self.run_init(answers=["my-model", "", "my-reviewer", "", ""])
        self.assertIn("Codex model list unavailable", self.output.getvalue())
        self.assertEqual(config.models["implementation"].name, "my-model")
        self.assertEqual(config.models["review"].name, "my-reviewer")

    def test_without_terminal_missing_values_stay_placeholders(self):
        del self.responses[("gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef")]
        del self.responses[("gh", "api", "user", "--jq", ".login")]
        config = self.run_init()
        self.assertEqual(self.prompts, [])
        self.assertTrue(check.placeholder([config.repository, config.trusted_authors]))
        self.assertTrue(check.placeholder(config.models["implementation"].name))
        # The preselected backup on the other engine also waits for its model.
        self.assertEqual(config.backup.engine, "codex")
        self.assertTrue(check.placeholder(config.backup.name))
        self.assertIn("Values to fill in", self.output.getvalue())
        self.assertIn("CHOOSE_MODEL_FOR_BACKUP", self.output.getvalue())

    def test_quality_policy_enables_checks(self):
        (self.root / ".github/scripts").mkdir(parents=True)
        (self.root / init.QUALITY_MODULE).write_text("")
        (self.root / init.QUALITY_MANIFEST).write_text("{}")
        config = self.run_init(answers=[""] * 8)
        self.assertTrue(config.verification.local_checks and config.verification.ci_checks)

    def test_existing_config_is_kept_before_any_question(self):
        (self.root / "ralph.toml").write_text("keep")
        with self.assertRaisesRegex(init.InitError, r"already exists: change it with `ralph-kit configure`, or use --force to start over"):
            self.run_init(answers=[])
        self.assertEqual(self.prompts, [])
        self.assertEqual((self.root / "ralph.toml").read_text(), "keep")
        self.run_init(answers=[""] * 8, force=True)

    def test_config_option_writes_the_given_path_and_leaves_the_default_alone(self):
        (self.root / "ralph.toml").write_text("keep")
        (self.root / "variants").mkdir()
        config = self.run_init(answers=[""] * 8, config=pathlib.Path("variants/review.toml"))
        self.assertEqual(config.repository, "acme/widget")
        self.assertTrue((self.root / "variants/review.toml").is_file())
        self.assertEqual((self.root / "ralph.toml").read_text(), "keep")
        output = self.output.getvalue()
        self.assertIn(f"Wrote {self.root / 'variants/review.toml'}.", output)
        self.assertIn("Next step: commit variants/review.toml and .gitignore, then run `ralph-kit check --config variants/review.toml`.", output)

    def test_force_overwrites_only_the_given_path(self):
        (self.root / "ralph.toml").write_text("keep")
        (self.root / "other.toml").write_text("old")
        with self.assertRaisesRegex(init.InitError, r"other.toml already exists: change it with `ralph-kit configure --config other.toml`, or use --force"):
            self.run_init(answers=[], config=pathlib.Path("other.toml"))
        self.assertEqual(self.prompts, [])
        self.assertEqual((self.root / "other.toml").read_text(), "old")
        self.run_init(answers=[""] * 8, force=True, config=self.root / "other.toml")
        self.assertIn("[models.implementation]", (self.root / "other.toml").read_text())
        self.assertEqual((self.root / "ralph.toml").read_text(), "keep")
        self.assertIn("then run `ralph-kit check --config other.toml`.", self.output.getvalue())

    def test_config_in_a_missing_directory_stops_before_any_question(self):
        with self.assertRaisesRegex(init.InitError, "missing/ does not exist"):
            self.run_init(answers=[], config=pathlib.Path("missing/ralph.toml"))
        self.assertEqual(self.prompts, [])

    def test_must_run_from_repository_root(self):
        self.responses[("git", "rev-parse", "--show-toplevel")] = str(self.root.parent)
        with self.assertRaisesRegex(init.InitError, "root of a Git repository"):
            self.run_init(answers=[])
        self.assertFalse((self.root / "ralph.toml").exists())

    def test_missing_gitignore_is_created_with_the_diagnostics_entry(self):
        self.run_init(answers=[""] * 8)
        self.assertEqual(self.gitignore(), ".ralph/\n")
        self.assertIn("Next step: commit ralph.toml and .gitignore, then run `ralph-kit check`.", self.output.getvalue())

    def test_entry_is_appended_keeping_content_and_line_endings(self):
        for existing, expected in (
            ("dist/\n", "dist/\n.ralph/\n"),
            ("dist/", "dist/\n.ralph/\n"),
            ("dist/\r\nbuild/", "dist/\r\nbuild/\r\n.ralph/\r\n"),
        ):
            with self.subTest(existing=existing):
                (self.root / ".gitignore").write_bytes(existing.encode())
                self.run_init(answers=[""] * 8, force=True)
                self.assertEqual(self.gitignore(), expected)
                self.assertIn("Added .ralph/ to .gitignore", self.output.getvalue())

    def test_gitignore_is_unchanged_when_git_already_ignores_diagnostics(self):
        for path, rules in (
            (".gitignore", "/.ralph\n"),
            (".gitignore", ".r*/**\n"),
            (".git/info/exclude", ".ralph/\n"),
        ):
            with self.subTest(path=path, rules=rules):
                self.output = io.StringIO()
                (self.root / ".gitignore").unlink(missing_ok=True)
                (self.root / ".git/info/exclude").write_text("")
                (self.root / path).write_text(rules)
                self.run_init(answers=[""] * 8, force=True)
                self.assertEqual(self.gitignore(), rules if path == ".gitignore" else None)
                self.assertNotIn("Added .ralph/", self.output.getvalue())
                self.assertIn("Next step: commit ralph.toml, then run", self.output.getvalue())

    def test_gitignore_is_unchanged_when_nothing_is_written(self):
        (self.root / ".gitignore").write_text("dist/\n")
        for name, configuration, tools, error in (
            ("existing configuration", "keep", {"git", "gh"}, "already exists"),
            ("interrupted questions", None, {"git", "gh"}, "init cancelled"),
            ("missing prerequisites", None, {"git"}, "gh not found"),
        ):
            with self.subTest(name):
                (self.root / "ralph.toml").unlink(missing_ok=True)
                if configuration:
                    (self.root / "ralph.toml").write_text(configuration)
                self.tools = tools
                with self.assertRaisesRegex((init.InitError, init.InitAborted), error):
                    self.run_init(answers=["1"])
                self.assertEqual(self.gitignore(), "dist/\n")

    def test_interrupted_questions_write_nothing(self):
        with self.assertRaises(init.InitAborted):
            self.run_init(answers=["1"])
        self.assertFalse((self.root / "ralph.toml").exists())


class RenderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name).resolve()

    def load(self, contents, **options):
        path = self.root / "ralph.toml"
        path.write_text(contents, encoding="utf-8")
        return load_config(path, self.root, **options)

    def test_a_loaded_configuration_is_rendered_with_its_own_values(self):
        self.assertEqual(init.render(self.load(CUSTOM_CONFIGURATION), self.root), CUSTOM_CONFIGURATION)

    def test_rendering_a_rendered_configuration_again_yields_the_same_file(self):
        for name, contents in (("default", DEFAULT_CONFIGURATION), ("custom", CUSTOM_CONFIGURATION)):
            with self.subTest(name):
                rendered = init.render(self.load(contents), self.root)
                self.assertEqual(init.render(self.load(rendered), self.root), rendered)

    def test_hand_written_configuration_is_rendered_in_the_renderer_layout(self):
        # Unsorted authors and an implicit engine: the renderer writes them its own way.
        hand_written = (KIT_ROOT / "tests/fixtures/ralph.toml").read_text().replace(
            'trusted_authors = ["lmpaul"]', 'trusted_authors = ["zoe", "lmpaul"]'
        ).replace('[models.review]\nengine = "codex"\n', "[models.review]\n")
        self.assertIn('["zoe", "lmpaul"]', hand_written)
        self.assertIn("[models.review]\nname", hand_written)
        rendered = init.render(self.load(hand_written), self.root)
        self.assertIn('trusted_authors = ["lmpaul", "zoe"]\n', rendered)
        self.assertIn('[models.review]\nengine = "codex"\nname = "gpt-5.6-sol"\n', rendered)
        self.assertEqual(init.render(self.load(rendered), self.root), rendered)

    def test_a_configuration_without_review_is_rendered_without_it(self):
        start, review = CUSTOM_CONFIGURATION.split("# Used by reviewed runs", 1)
        contents = start + review.split("\n\n", 1)[1]
        self.assertEqual(init.render(self.load(contents, require_review=False), self.root), contents)


if __name__ == "__main__":
    unittest.main()
