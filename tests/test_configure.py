"""`ralph-kit configure` without real git, gh, or terminal."""

import io
import itertools
import pathlib
import subprocess
import sys
import tempfile
import unittest


KIT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT / "src"))

from ralph_kit import configure, init
from ralph_kit.config import load_config


sys.path.insert(0, str(KIT_ROOT / "tests"))
from test_init import CUSTOM_CONFIGURATION, DEFAULT_CONFIGURATION  # noqa: E402


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.responses = {}
        self.output = io.StringIO()
        self.write(DEFAULT_CONFIGURATION)

    def write(self, contents, name="ralph.toml"):
        (self.root / name).write_text(contents, encoding="utf-8")

    def read(self, name="ralph.toml"):
        return (self.root / name).read_text(encoding="utf-8")

    def run_configure(self, answers=None, config=None):
        """answers=None simulates a non-interactive stdin; otherwise one answer per prompt."""
        pending = list(answers or [])
        self.prompts = []

        def read(prompt):
            self.prompts.append(prompt)
            if not pending:
                raise EOFError
            answer = pending.pop(0)
            if answer is KeyboardInterrupt:
                raise KeyboardInterrupt
            return answer

        prompter = init.Prompter(interactive=answers is not None, read=read, output=self.output)
        options = {} if config is None else {"config": config}
        return configure.run(
            self.root, prompter, command=lambda *args: self.responses.get(args), which=lambda name: None, **options
        )

    def menu(self):
        """The options of the last menu shown, one line each."""
        shown = self.output.getvalue().rsplit("what do you want to change?", 1)[1].splitlines()[1:]
        return "\n".join(itertools.takewhile(lambda line: line.startswith("  ") and ")" in line, shown))

    def test_without_a_terminal_it_refuses_and_writes_nothing(self):
        with self.assertRaisesRegex(configure.ConfigureError, "needs an interactive terminal; edit ralph.toml directly"):
            self.run_configure()
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION)

    def test_a_missing_configuration_points_to_init(self):
        (self.root / "ralph.toml").unlink()
        with self.assertRaisesRegex(configure.ConfigureError, "run `ralph-kit init` first"):
            self.run_configure(answers=[])
        self.assertEqual(self.prompts, [])

    def test_an_invalid_configuration_is_refused_with_the_way_to_start_over(self):
        for config, suggestion in (
            (None, "Fix ralph.toml, or start over with `ralph-kit init --force`."),
            (pathlib.Path("b.toml"), "Fix b.toml, or start over with `ralph-kit init --config b.toml --force`."),
        ):
            with self.subTest(config=config):
                invalid = DEFAULT_CONFIGURATION.replace('remote = "origin"', 'remote = "-x"')
                self.write(invalid, name=str(config or "ralph.toml"))
                with self.assertRaises(configure.ConfigureError) as raised:
                    self.run_configure(answers=[], config=config)
                self.assertIn("configuration.remote is not a safe Git name", str(raised.exception))
                self.assertIn(suggestion, str(raised.exception))
                self.assertEqual(self.prompts, [])
                self.assertEqual(self.read(str(config or "ralph.toml")), invalid)

    def test_the_menu_summarises_each_section(self):
        self.run_configure(answers=["7"])
        menu = self.menu()
        self.assertIn("? ralph.toml — what do you want to change?", self.output.getvalue())
        lines = [line.strip() for line in menu.strip().splitlines()]
        self.assertEqual(lines[0], "1)   Model profiles  (implementation claude/sonnet medium · review claude/opus high · correction = implementation)  (unavailable: not editable yet: edit the file directly)")
        self.assertTrue(lines[1].startswith("2)   Backup  (codex/codex-large medium)"))
        self.assertTrue(lines[2].startswith("3)   Trusted authors  (octocat)"))
        self.assertEqual(lines[3], "4)   Verification  (local off · CI off)")
        self.assertTrue(lines[4].startswith("5)   Git  (acme/widget · trunk · origin)"))
        self.assertEqual(lines[5:], ["6)   Save", "7)   Quit without saving"])

    def test_quit_without_saving_writes_nothing(self):
        written = self.run_configure(answers=["4", "1", "2", "", "7"])
        self.assertFalse(written)
        self.assertIn("* Verification  (local on · CI on)", self.menu())
        self.assertIn("Nothing written.", self.output.getvalue())
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION)

    def test_interruption_writes_nothing(self):
        with self.assertRaisesRegex(configure.ConfigureAborted, "nothing written"):
            self.run_configure(answers=["4", "1", KeyboardInterrupt])
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION)

    def test_save_without_change_writes_nothing(self):
        before = (self.root / "ralph.toml").stat().st_mtime_ns
        self.assertFalse(self.run_configure(answers=["4", "1", "1", "", ""]))  # On then off again, then Save.
        self.assertIn("No changes: nothing written.", self.output.getvalue())
        self.assertEqual((self.root / "ralph.toml").stat().st_mtime_ns, before)

    def test_verification_toggles_each_check_and_warns_without_the_quality_policy(self):
        self.assertTrue(self.run_configure(answers=["4", "2", "", "6"]))
        config = load_config(self.root / "ralph.toml", self.root)
        self.assertFalse(config.verification.local_checks)
        self.assertTrue(config.verification.ci_checks)
        output = self.output.getvalue()
        self.assertIn("⚠ quality.py and quality-gates.json missing: `ralph-kit check` will fail until they exist.", output)
        self.assertIn("Changes:\n  verification.ci_checks: false → true\n", output)
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION.replace("ci_checks = false", "ci_checks = true"))
        self.assertIn("Next step: commit ralph.toml, then run `ralph-kit check`.", output)

    def test_no_warning_when_the_quality_policy_exists(self):
        (self.root / ".github/scripts").mkdir(parents=True)
        (self.root / init.QUALITY_MODULE).write_text("")
        (self.root / init.QUALITY_MANIFEST).write_text("{}")
        self.run_configure(answers=["4", "1", "", "6"])
        self.assertNotIn("⚠", self.output.getvalue())
        self.assertTrue(load_config(self.root / "ralph.toml", self.root).verification.local_checks)

    def test_a_save_keeps_every_value_it_did_not_change(self):
        # Custom labels, quality policy paths, display names, a distinct correction, several authors, another remote.
        self.write(CUSTOM_CONFIGURATION)
        self.run_configure(answers=["4", "1", "", "6"])
        self.assertEqual(self.read(), CUSTOM_CONFIGURATION.replace("local_checks = true", "local_checks = false"))
        self.assertIn("Changes:\n  verification.local_checks: true → false\n\n", self.output.getvalue())
        self.assertNotIn("⚠", self.output.getvalue())  # Turning a check off needs no quality policy.

    def test_a_configuration_without_review_stays_without_it(self):
        start, review = CUSTOM_CONFIGURATION.split("# Only used by", 1)
        contents = start + review.split("\n\n", 1)[1]
        self.write(contents)
        self.run_configure(answers=["4", "2", "", "6"])
        self.assertEqual(self.read(), contents.replace("ci_checks = false", "ci_checks = true"))

    def test_comments_the_renderer_would_not_write_are_announced_once(self):
        self.write("# Tuned by hand\n" + DEFAULT_CONFIGURATION.replace('remote = "origin"', 'remote = "origin"  # fork'))
        self.run_configure(answers=["4", "1", "", "6"])
        output = self.output.getvalue()
        self.assertEqual(output.count("has comments that ralph-kit does not write: saving will drop them"), 1)
        self.assertNotIn("Tuned by hand", self.read())

    def test_rendered_comments_and_hashes_in_strings_are_not_announced(self):
        self.write(DEFAULT_CONFIGURATION.replace('"needs-triage"', '"needs #triage"'))
        self.run_configure(answers=["7"])
        self.assertNotIn("comments", self.output.getvalue())

    def test_config_option_edits_another_configuration(self):
        self.write(DEFAULT_CONFIGURATION, name="b.toml")
        self.run_configure(answers=["4", "1", "", "6"], config=pathlib.Path("b.toml"))
        self.assertIn("? b.toml — what do you want to change?", self.output.getvalue())
        self.assertTrue(load_config(self.root / "b.toml", self.root).verification.local_checks)
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION)
        self.assertIn("Next step: commit b.toml, then run `ralph-kit check --config b.toml`.", self.output.getvalue())

    def test_unavailable_sections_are_refused_by_number(self):
        self.run_configure(answers=["1", "7"])
        self.assertIn("Model profiles is unavailable", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
