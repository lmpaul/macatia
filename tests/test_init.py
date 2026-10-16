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
            ("gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef"): json.dumps(
                {"nameWithOwner": "acme/widget", "defaultBranchRef": {"name": "trunk"}}
            ),
            ("git", "remote"): "origin",
            ("gh", "api", "user", "--jq", ".login"): "octocat",
        }
        self.installed = {"claude", "codex"}
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
        path = init.run(self.root, prompter, force=force, command=self.command, which=lambda name: name if name in self.installed else None)
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
            "",                           # engine: installed codex is the default
            "", "high",                   # implementation model and effort
            "custom-review", "",          # review model and default effort
        ])
        self.assertEqual(config.repository, "acme/widget")
        self.assertIn("format attendu", self.output.getvalue())
        self.assertEqual(config.base_branch, "main")
        self.assertEqual(config.remote, "upstream")
        self.assertEqual(config.trusted_authors, {"alice", "bob"})
        self.assertEqual(config.models["implementation"].engine, "codex")
        self.assertEqual(config.models["implementation"].name, "gpt-5.6-terra")
        self.assertEqual(config.models["implementation"].reasoning_effort, "high")
        self.assertEqual(config.models["review"].name, "custom-review")

    def test_without_terminal_missing_values_stay_placeholders(self):
        del self.responses[("gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef")]
        del self.responses[("gh", "api", "user", "--jq", ".login")]
        config = self.run_init()
        self.assertEqual(self.prompts, [])
        self.assertTrue(check.placeholder([config.repository, config.trusted_authors]))
        self.assertTrue(check.placeholder(config.models["implementation"].name))
        self.assertIn("Valeurs à compléter", self.output.getvalue())

    def test_quality_policy_enables_checks(self):
        (self.root / ".github/scripts").mkdir(parents=True)
        (self.root / init.QUALITY_MODULE).write_text("")
        (self.root / init.QUALITY_MANIFEST).write_text("{}")
        config = self.run_init(answers=[""] * 5)
        self.assertTrue(config.verification.local_checks and config.verification.ci_checks)

    def test_existing_config_is_kept_before_any_question(self):
        (self.root / "ralph.toml").write_text("keep")
        with self.assertRaisesRegex(init.InitError, "existe déjà"):
            self.run_init(answers=[])
        self.assertEqual(self.prompts, [])
        self.assertEqual((self.root / "ralph.toml").read_text(), "keep")
        self.run_init(answers=[""] * 5, force=True)

    def test_must_run_from_repository_root(self):
        self.responses[("git", "rev-parse", "--show-toplevel")] = str(self.root.parent)
        with self.assertRaisesRegex(init.InitError, "racine"):
            self.run_init(answers=[])
        self.assertFalse((self.root / "ralph.toml").exists())

    def test_interrupted_questions_write_nothing(self):
        with self.assertRaises(init.InitAborted):
            self.run_init(answers=["1"])
        self.assertFalse((self.root / "ralph.toml").exists())


if __name__ == "__main__":
    unittest.main()
