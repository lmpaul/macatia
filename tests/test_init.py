"""`ralph-kit init` without real git, gh, or terminal."""

import io
import json
import pathlib
import sys
import tempfile
import unittest

KIT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT / "src"))

from ralph_kit import check, init
from ralph_kit.config import load_config


class InitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name).resolve()
        self.responses = {
            ("git", "rev-parse", "--show-toplevel"): str(self.root),
            ("gh", "auth", "status"): "",
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

    def command(self, *args):
        return self.responses.get(args)

    def run_init(self, answers=None, force=False):
        """answers=None simulates a non-interactive stdin; otherwise one answer per prompt."""
        pending = list(answers or [])
        self.prompts = []

        def read(prompt):
            self.prompts.append(prompt)
            if not pending:
                raise EOFError
            return pending.pop(0)

        prompter = init.Prompter(interactive=answers is not None, read=read, output=self.output)
        path = init.run(self.root, prompter, force=force, command=self.command, which=lambda name: name if name in self.installed | self.tools else None)
        return load_config(path, self.root)

    def test_detected_values_are_not_asked(self):
        # Only the engine, two models and two efforts are asked; Enter keeps every default.
        config = self.run_init(answers=[""] * 5)
        self.assertEqual(len(self.prompts), 5)
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
        self.run_init(answers=[""] * 4)
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
        self.assertEqual(len(asked), 5)
        self.assertEqual(config.models["implementation"].reasoning_effort, "max")
        self.assertNotIn("Choice [", self.output.getvalue())

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

    def test_unauthenticated_gh_stops_before_questions(self):
        del self.responses[("gh", "auth", "status")]
        with self.assertRaisesRegex(init.InitError, "gh auth login"):
            self.run_init(answers=[])
        self.assertEqual(self.prompts, [])

    def test_codex_catalog_drives_models_and_efforts(self):
        self.installed = {"codex"}
        config = self.run_init(answers=["", "", "2", ""])
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
        config = self.run_init(answers=["my-model", "", "my-reviewer", ""])
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
        self.assertIn("Values to fill in", self.output.getvalue())

    def test_quality_policy_enables_checks(self):
        (self.root / ".github/scripts").mkdir(parents=True)
        (self.root / init.QUALITY_MODULE).write_text("")
        (self.root / init.QUALITY_MANIFEST).write_text("{}")
        config = self.run_init(answers=[""] * 5)
        self.assertTrue(config.verification.local_checks and config.verification.ci_checks)

    def test_existing_config_is_kept_before_any_question(self):
        (self.root / "ralph.toml").write_text("keep")
        with self.assertRaisesRegex(init.InitError, "already exists"):
            self.run_init(answers=[])
        self.assertEqual(self.prompts, [])
        self.assertEqual((self.root / "ralph.toml").read_text(), "keep")
        self.run_init(answers=[""] * 5, force=True)

    def test_must_run_from_repository_root(self):
        self.responses[("git", "rev-parse", "--show-toplevel")] = str(self.root.parent)
        with self.assertRaisesRegex(init.InitError, "root of a Git repository"):
            self.run_init(answers=[])
        self.assertFalse((self.root / "ralph.toml").exists())

    def test_interrupted_questions_write_nothing(self):
        with self.assertRaises(init.InitAborted):
            self.run_init(answers=["1"])
        self.assertFalse((self.root / "ralph.toml").exists())


if __name__ == "__main__":
    unittest.main()
