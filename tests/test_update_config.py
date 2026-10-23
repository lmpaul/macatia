"""ralph-kit update-config: write the defaulted settings into the configuration, then the labels on GitHub."""

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

from ralph_kit import cli, config, init, update_config


ALL_LABELS = [
    "needs-triage", "needs-info", "ready-for-agent", "ready-for-human", "wontfix",
    "agent-working", "human-review", "implemented", "work-started", "standalone",
]


REVIEW = """\
# true makes every run a reviewed run; ralph-kit run --no-review overrides it.
review_by_default = false
"""
CAFFEINATE = """\
# true makes every run hold a sleep lock; ralph-kit run --no-caffeinate overrides it.
caffeinate_by_default = false
"""
TOP_LEVEL = REVIEW + CAFFEINATE


class UpdateConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name).resolve()
        self.config = self.root / "ralph.toml"
        shutil.copy2(FIXTURES / "ralph.toml", self.config)
        self.labels = list(ALL_LABELS)
        self.commands = []
        self.declare_top_level_settings()

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
            code = update_config.run(self.root, self.config, confirm=confirm, command=self.command)
        return code, output.getvalue()

    def declare_top_level_settings(self):
        """The fixture with both defaulted top-level settings, as init writes them."""
        text = self.config.read_text().replace('trusted_authors = ["lmpaul"]\n', 'trusted_authors = ["lmpaul"]\n' + TOP_LEVEL)
        self.config.write_text(text)
        return text

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
        self.assertIn("✓ ralph.toml: nothing to add, every defaulted setting is declared", output)
        self.assertNotIn("added", output)
        self.assertIn("✓ all 10 labels present on lmpaul/buyr", output)
        self.assertNotIn("Commit ralph.toml", output)

    def test_missing_top_level_settings_are_added_before_the_first_table_with_their_comments(self):
        declared = self.config.read_text()
        for kept, completed, added in (
            ("", TOP_LEVEL, ["review_by_default", "caffeinate_by_default"]),
            (REVIEW, TOP_LEVEL, ["caffeinate_by_default"]),
            (CAFFEINATE, CAFFEINATE + REVIEW, ["review_by_default"]),
        ):
            with self.subTest(added=added):
                self.config.write_text(declared.replace(TOP_LEVEL, kept))

                code, output = self.run_update()

                self.assertEqual(code, 0, output)
                self.assertEqual(self.config.read_text(), declared.replace(TOP_LEVEL, completed))
                self.assertEqual(
                    [line for line in output.splitlines() if "added" in line],
                    [f"✓ ralph.toml: added {key} = false" for key in added],
                )
                self.assertIn("✓ all 10 labels present on lmpaul/buyr", output)
                self.assertIn("Commit ralph.toml", output)

    def test_a_file_init_wrote_gets_back_the_lines_init_writes(self):
        rendered = init.render(config.load_config(self.config, self.root, require_review=False), self.root)
        self.config.write_text(rendered.replace(TOP_LEVEL, ""))

        code, output = self.run_update()

        self.assertEqual(code, 0, output)
        self.assertEqual(self.config.read_text(), rendered)

    def test_top_level_settings_go_after_the_last_setting_and_keep_hand_written_comments(self):
        text = self.config.read_text().replace(TOP_LEVEL, "")
        text = text.replace('version = 1\n', '# Our Ralph setup.\nversion = 1  # format\n')
        text = text.replace('trusted_authors = ["lmpaul"]\n', 'trusted_authors = [\n  "lmpaul",  # owner\n]\n# Labels follow.\n\n')
        text = text.replace("[labels]\n", "# Our names.\n[labels]\n")
        self.config.write_text(text)

        code, output = self.run_update()

        self.assertEqual(code, 0, output)
        self.assertEqual(
            self.config.read_text(),
            text.replace('# Labels follow.\n', '# Labels follow.\n' + TOP_LEVEL),
        )

    def test_top_level_settings_and_label_roles_are_added_together(self):
        text = self.config.read_text().replace(TOP_LEVEL, "").replace('standalone = "standalone"\n', "")
        self.config.write_text(text)

        code, output = self.run_update()

        self.assertEqual(code, 0, output)
        self.assertEqual(
            self.config.read_text(),
            text.replace('trusted_authors = ["lmpaul"]\n', 'trusted_authors = ["lmpaul"]\n' + TOP_LEVEL)
            .replace('work_started = "work-started"\n', 'work_started = "work-started"\nstandalone = "standalone"\n'),
        )
        self.assertIn(
            "✓ ralph.toml: added review_by_default = false\n"
            "✓ ralph.toml: added caffeinate_by_default = false\n"
            '✓ ralph.toml: added standalone = "standalone"\n',
            output,
        )

    def test_optional_model_profiles_left_out_stay_absent(self):
        text = self.config.read_text()
        start, end = text.index("[models.review]"), text.index("[models.correction]")
        text = (text[:start] + text[end:]).replace(TOP_LEVEL, "")
        self.config.write_text(text)

        code, output = self.run_update()

        self.assertEqual(code, 0, output)
        completed = self.config.read_text()
        self.assertNotIn("[models.review]", completed)
        self.assertNotIn("[models.backup]", completed)
        self.assertNotIn("models", output)

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
        text = self.config.read_text().replace('remote = "origin"', 'remote = "-x"')
        self.config.write_text(text)

        code, output = self.run_update()

        self.assertEqual(code, 1)
        self.assertEqual(self.config.read_bytes(), text.encode())
        self.assertIn("configuration.remote is not a safe Git name", output)
        self.assertNotIn("added", output)
        self.assertEqual(self.commands, [])
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["ralph.toml"])

    def test_a_missing_setting_without_a_default_leaves_the_file_unchanged(self):
        self.remove_line('work_started = "work-started"\n')
        self.remove_line('remote = "origin"\n')
        text = self.config.read_text()

        code, output = self.run_update()

        self.assertEqual(code, 1)
        self.assertEqual(self.config.read_bytes(), text.encode())
        self.assertIn("missing keys: remote", output)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["ralph.toml"])

    def test_no_staged_copy_is_left_once_the_completed_file_is_written(self):
        self.remove_line('work_started = "work-started"\n')

        code, output = self.run_update()

        self.assertEqual(code, 0, output)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["ralph.toml"])

    def test_the_command_is_dispatched_with_its_own_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(["update-config", "--bogus"])
        self.assertEqual(raised.exception.code, 2)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main(["--help"])
        self.assertIn("update-config [--config PATH]", output.getvalue())
        self.assertNotIn("update-labels", output.getvalue())

    def test_the_old_command_name_is_a_usage_error(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            code = cli.main(["update-labels"])
        self.assertEqual(code, 2)
        self.assertIn("unknown command 'update-labels'", output.getvalue())
        self.assertIn("update-config [--config PATH]", output.getvalue())


if __name__ == "__main__":
    unittest.main()
