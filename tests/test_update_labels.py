"""ralph-kit update-labels: complete [labels] in the configuration, then the labels on GitHub."""

import contextlib
import io
import json
import pathlib
import shutil
import sys
import tempfile
import unittest


KIT_ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = KIT_ROOT / "tests" / "fixtures"
sys.path.insert(0, str(KIT_ROOT / "src"))

from ralph_kit import cli, update_labels


ALL_LABELS = [
    "needs-triage", "needs-info", "ready-for-agent", "ready-for-human", "wontfix",
    "agent-working", "human-review", "implemented", "work-started", "standalone",
]


class UpdateLabelsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name).resolve()
        self.config = self.root / "ralph.toml"
        shutil.copy2(FIXTURES / "ralph.toml", self.config)
        self.labels = list(ALL_LABELS)
        self.commands = []

    def command(self, *args):
        self.commands.append(args)
        if args == ("gh", "api", "--paginate", "--slurp", "repos/lmpaul/buyr/labels"):
            return json.dumps([[{"name": name} for name in self.labels]])
        if args[:3] == ("gh", "label", "create"):
            self.labels.append(args[3])
            return ""
        raise AssertionError(f"unexpected command {args}")

    def remove_line(self, line):
        text = self.config.read_text()
        assert line in text, line
        self.config.write_text(text.replace(line, ""))

    def run_update(self, confirm=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = update_labels.run(self.root, self.config, confirm=confirm, command=self.command)
        return code, output.getvalue()

    def test_a_missing_label_role_is_added_at_the_end_of_labels_and_nothing_else_changes(self):
        text = self.config.read_text().replace('work_started = "work-started"\n', "")
        text = text.replace("[labels]\n", "[labels]\n# Our own names.\n")
        self.config.write_text(text)

        code, output = self.run_update()

        self.assertEqual(code, 0, output)
        self.assertEqual(
            self.config.read_text(),
            text.replace('standalone = "standalone"\n', 'standalone = "standalone"\nwork_started = "work-started"\n'),
        )
        self.assertIn('✓ ralph.toml: added work_started = "work-started"', output)
        self.assertIn("✓ all 10 labels present on lmpaul/buyr", output)
        self.assertIn("Commit ralph.toml", output)

    def test_a_configuration_without_standalone_gets_it_and_its_github_label(self):
        self.remove_line('standalone = "standalone"\n')
        self.labels.remove("standalone")

        code, output = self.run_update(confirm=lambda question: True)

        self.assertEqual(code, 0, output)
        self.assertIn('work_started = "work-started"\nstandalone = "standalone"\n', self.config.read_text())
        self.assertIn('✓ ralph.toml: added standalone = "standalone"', output)
        self.assertIn(
            ("gh", "label", "create", "standalone", "--repo", "lmpaul/buyr",
             "--description", "Ralph has started work on this standalone issue"),
            self.commands,
        )
        self.assertIn("✓ standalone created", output)

    def test_a_configuration_refused_for_missing_label_keys_is_completed(self):
        self.remove_line('agent_working = "agent-working"\n')
        self.remove_line('work_started = "work-started"\n')

        code, output = self.run_update()

        self.assertEqual(code, 0, output)
        self.assertIn('standalone = "standalone"\nagent_working = "agent-working"\nwork_started = "work-started"\n\n[models', self.config.read_text())

    def test_labels_missing_on_github_are_created_once_confirmed(self):
        self.labels.remove("work-started")
        questions = []

        code, output = self.run_update(confirm=lambda question: questions.append(question) or True)

        self.assertEqual(code, 0, output)
        self.assertEqual(questions, ["Create the 1 missing label on lmpaul/buyr?"])
        self.assertIn(
            ("gh", "label", "create", "work-started", "--repo", "lmpaul/buyr",
             "--description", "Ralph has started work on this parent"),
            self.commands,
        )
        self.assertIn("✓ work-started created", output)
        self.assertNotIn("Commit ralph.toml", output)

    def test_labels_missing_on_github_are_left_without_a_terminal_or_for_later(self):
        self.labels.remove("work-started")
        for confirm in (None, lambda question: False):
            with self.subTest(confirm=confirm):
                code, output = self.run_update(confirm=confirm)

                self.assertEqual(code, 1)
                self.assertIn("1 missing on lmpaul/buyr: work-started", output)
                self.assertIn("gh label create work-started", output)
                self.assertFalse(any(args[:3] == ("gh", "label", "create") for args in self.commands))

    def test_nothing_to_do_leaves_the_file_alone(self):
        original = self.config.read_text()

        code, output = self.run_update()

        self.assertEqual(code, 0, output)
        self.assertEqual(self.config.read_text(), original)
        self.assertIn("✓ ralph.toml declares all 10 label roles", output)

    def test_a_default_name_another_role_uses_is_refused(self):
        self.remove_line('work_started = "work-started"\n')
        text = self.config.read_text().replace('wontfix = "wontfix"', 'wontfix = "work-started"')
        self.config.write_text(text)

        code, output = self.run_update()

        self.assertEqual(code, 1)
        self.assertIn("work-started is already the label of wontfix", output)
        self.assertEqual(self.config.read_text(), text)
        self.assertEqual(self.commands, [])

    def test_labels_outside_a_labels_table_are_left_for_a_fix_by_hand(self):
        text = self.config.read_text()
        start, end = text.index("[labels]\n"), text.index("[models.implementation]")
        inline = 'labels = { needs_triage = "needs-triage" }\n'
        text = text[:start].replace('trusted_authors = ["lmpaul"]\n', f'trusted_authors = ["lmpaul"]\n{inline}') + text[end:]
        self.config.write_text(text)

        code, output = self.run_update()

        self.assertEqual(code, 1)
        self.assertIn("no [labels] table", output)
        self.assertEqual(self.config.read_text(), text)

    def test_a_missing_configuration_points_to_init(self):
        self.config.unlink()

        code, output = self.run_update()

        self.assertEqual(code, 1)
        self.assertIn("run `ralph-kit init` first", output)

    def test_another_configuration_problem_is_reported_after_the_labels_are_added(self):
        self.remove_line('work_started = "work-started"\n')
        self.config.write_text(self.config.read_text().replace('remote = "origin"', 'remote = "-x"'))

        code, output = self.run_update()

        self.assertEqual(code, 1)
        self.assertIn('work_started = "work-started"', self.config.read_text())
        self.assertIn("configuration.remote is not a safe Git name", output)
        self.assertEqual(self.commands, [])

    def test_the_command_is_dispatched_with_its_own_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(["update-labels", "--bogus"])
        self.assertEqual(raised.exception.code, 2)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main(["--help"])
        self.assertIn("update-labels [--config PATH]", output.getvalue())


if __name__ == "__main__":
    unittest.main()
