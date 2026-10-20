"""`ralph-kit configure` without real git, gh, or terminal."""

import io
import itertools
import json
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


GH_LOGIN = ("gh", "api", "user", "--jq", ".login")
GH_REPOSITORY = ("gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef")
GIT_REMOTES = ("git", "remote")


def labels_of(repository):
    return ("gh", "api", "--paginate", "--slurp", f"repos/{repository}/labels")


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.responses = {}
        self.command = lambda *args: self.responses.get(args)
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
            self.root, prompter, command=lambda *args: self.command(*args), which=lambda name: None, **options
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
        self.assertEqual(lines[4], "5)   Git  (acme/widget · trunk · origin)")
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
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [".git", "ralph.toml"])

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

    def test_trusted_authors_lists_them_and_adds_one_without_duplicates(self):
        self.write(CUSTOM_CONFIGURATION)
        # Add zoe, then alice again, then Back and Save.
        self.assertTrue(self.run_configure(answers=["3", "1", "zoe smith", "zoe", "1", "alice", "", "6"]))
        output = self.output.getvalue()
        self.assertIn("? Trusted authors: alice, bob, octocat — what do you want to do?", output)
        self.assertIn("  ✓ zoe added", output)
        self.assertIn("  a GitHub account name, without spaces", output)
        self.assertIn("  alice is already a trusted author", output)
        self.assertIn("* Trusted authors  (alice, bob, octocat, zoe)", self.menu())
        self.assertIn("Changes:\n  trusted_authors: alice, bob, octocat → alice, bob, octocat, zoe\n", output)
        self.assertEqual(
            self.read(),
            CUSTOM_CONFIGURATION.replace('["alice", "bob", "octocat"]', '["alice", "bob", "octocat", "zoe"]'),
        )

    def test_adding_nothing_goes_back(self):
        self.assertFalse(self.run_configure(answers=["3", "1", "", "", "6"]))
        self.assertIn("GitHub account to trust (leave empty to go back)", self.output.getvalue())
        self.assertIn("No changes: nothing written.", self.output.getvalue())

    def test_trusted_authors_removes_one(self):
        self.write(CUSTOM_CONFIGURATION)
        # Remove bob (second of alice, bob, octocat), then Back and Save.
        self.assertTrue(self.run_configure(answers=["3", "2", "2", "", "6"]))
        self.assertIn("  ✓ bob removed", self.output.getvalue())
        self.assertEqual(load_config(self.root / "ralph.toml", self.root).trusted_authors, {"alice", "octocat"})
        self.assertEqual(self.read(), CUSTOM_CONFIGURATION.replace('["alice", "bob", "octocat"]', '["alice", "octocat"]'))

    def test_removing_the_last_trusted_author_is_refused(self):
        self.assertFalse(self.run_configure(answers=["3", "2", "", "6"]))
        output = self.output.getvalue()
        self.assertIn("  ✗ octocat is the last trusted author: add another one before removing it.", output)
        self.assertIn("No changes: nothing written.", output)
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION)

    def test_a_gh_login_that_is_not_trusted_is_warned_about(self):
        warning = "  ⚠ The current gh account, hubot, is not a trusted author: Ralph would ignore its own implementation evidence."
        self.responses[GH_LOGIN] = "hubot"
        self.run_configure(answers=["3", "1", "hubot", "", "7"])
        output = self.output.getvalue()
        self.assertEqual(output.count(warning), 1)  # Not after hubot was added.
        self.assertLess(output.index(warning), output.index("? Trusted authors: octocat —"))

    def test_removing_the_gh_login_warns(self):
        self.write(CUSTOM_CONFIGURATION)
        self.responses[GH_LOGIN] = "octocat"
        self.run_configure(answers=["3", "2", "3", "", "7"])
        self.assertIn("  ✓ octocat removed\n  ⚠ The current gh account, octocat, is not a trusted author", self.output.getvalue())

    def test_no_warning_when_the_gh_login_is_trusted_or_gh_is_missing(self):
        for login in ("octocat", None):
            with self.subTest(login=login):
                self.output = io.StringIO()
                self.responses[GH_LOGIN] = login
                self.run_configure(answers=["3", "", "7"])
                self.assertNotIn("⚠", self.output.getvalue())

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

    def test_git_edits_the_repository_base_branch_and_remote(self):
        # Without gh or git output: no hint and no label offer, editing still works.
        answers = ["5", "1", "acme", "acme/gadget", "2", "-x", "develop", "3", "my fork", "upstream", "", "6"]
        self.assertTrue(self.run_configure(answers=answers))
        output = self.output.getvalue()
        self.assertIn("? GitHub repository (owner/name)\n", output)
        self.assertIn("Answer [acme/widget]: ", self.prompts)
        self.assertIn("  expected format: owner/name", output)
        self.assertEqual(output.count("  invalid Git name (no spaces, must not start with -)"), 2)
        self.assertNotIn("detected", output)
        self.assertNotIn("Labels", output)
        self.assertIn("* Git  (acme/gadget · develop · upstream)", self.menu())
        self.assertIn(
            "Changes:\n  repository: acme/widget → acme/gadget\n  base_branch: trunk → develop\n  remote: origin → upstream\n",
            output,
        )
        config = load_config(self.root / "ralph.toml", self.root)
        self.assertEqual((config.repository, config.base_branch, config.remote), ("acme/gadget", "develop", "upstream"))
        self.assertEqual(
            self.read(),
            DEFAULT_CONFIGURATION.replace('"acme/widget"', '"acme/gadget"')
            .replace('"trunk"', '"develop"')
            .replace('remote = "origin"', 'remote = "upstream"'),
        )

    def test_git_keeps_the_current_value_on_enter(self):
        self.write(CUSTOM_CONFIGURATION)
        self.responses[GH_REPOSITORY] = json.dumps({"nameWithOwner": "acme/widget", "defaultBranchRef": {"name": "main"}})
        self.responses[GIT_REMOTES] = "origin\nupstream"
        self.assertFalse(self.run_configure(answers=["5", "1", "", "2", "", "3", "", "", "6"]))
        self.assertEqual(
            [prompt for prompt in self.prompts if prompt.startswith("Answer")],
            ["Answer [acme/gadget]: ", "Answer [develop]: ", "Answer [upstream]: "],
        )
        self.assertIn("No changes: nothing written.", self.output.getvalue())
        self.assertEqual(self.read(), CUSTOM_CONFIGURATION)

    def test_git_shows_the_detected_value_when_it_differs(self):
        self.responses[GH_REPOSITORY] = json.dumps({"nameWithOwner": "acme/gadget", "defaultBranchRef": {"name": "main"}})
        self.responses[("git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")] = "origin/develop"
        self.responses[GIT_REMOTES] = "origin\nupstream"
        self.run_configure(answers=["5", "1", "", "2", "", "3", "", "", "7"])
        output = self.output.getvalue()
        self.assertIn("  1) Repository  (acme/widget; detected acme/gadget)\n", output)
        self.assertIn("? GitHub repository (owner/name); detected acme/gadget\n", output)
        # The gh default branch belongs to another repository: the remote's HEAD is used instead.
        self.assertIn("  2) Base branch  (trunk; detected develop)\n", output)
        self.assertIn("? Base branch (Ralph never commits to it); detected develop\n", output)
        self.assertIn("  3) Remote  (origin; Git remotes: origin, upstream)\n", output)
        self.assertIn("? Remote Ralph pushes to; Git remotes: origin, upstream\n", output)
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION)

    def test_git_hides_detected_values_that_match(self):
        self.responses[GH_REPOSITORY] = json.dumps({"nameWithOwner": "acme/widget", "defaultBranchRef": {"name": "trunk"}})
        self.responses[GIT_REMOTES] = "origin"
        self.run_configure(answers=["5", "", "7"])
        output = self.output.getvalue()
        self.assertIn("  1) Repository  (acme/widget)\n  2) Base branch  (trunk)\n  3) Remote  (origin)\n", output)

    def test_a_repository_change_offers_its_missing_labels_and_creates_them_after_writing(self):
        self.write(CUSTOM_CONFIGURATION)  # Custom label names are the ones looked for.
        self.responses[labels_of("acme/other")] = json.dumps([[{"name": "triage: new"}, {"name": "agent: ready"}]])
        created = []

        def command(*args):
            if args[:3] == ("gh", "label", "create"):
                self.assertIn('repository = "acme/other"', self.read())
                self.assertEqual(args[4:6], ("--repo", "acme/other"))
                created.append(args[3])
                return None if args[3] == "done" else ""
            return self.responses.get(args)

        self.command = command
        self.assertTrue(self.run_configure(answers=["5", "1", "acme/other", "", "", "6"]))  # Enter keeps "Yes".
        output = self.output.getvalue()
        self.assertIn("  ℹ Labels missing on acme/other: agent: working, done, human: ready, human: review, triage: info, won't fix", output)
        self.assertIn("? Create the 6 missing labels?", output)
        self.assertEqual(created, ["triage: info", "human: ready", "won't fix", "agent: working", "human: review", "done"])
        self.assertIn("   ✓ Label won't fix created\n", output)
        self.assertIn("   ✗ Label done not created\n", output)
        self.assertLess(output.index("✅ Wrote"), output.index("✓ Label"))

    def test_labels_kept_for_later_or_present_are_not_created(self):
        created = []

        def command(*args):
            if args[:3] == ("gh", "label", "create"):
                created.append(args[3])
            return self.responses.get(args)

        self.command = command
        self.responses[labels_of("acme/other")] = json.dumps([[]])
        self.responses[labels_of("acme/full")] = json.dumps([[{"name": name} for name in init.LABELS.values()]])
        self.run_configure(answers=["5", "1", "acme/other", "2", "", "6"])
        self.assertIn("? Create the 8 missing labels?", self.output.getvalue())
        self.run_configure(answers=["5", "1", "acme/full", "", "6"])
        self.assertIn("  ✓ Labels: all 8 present", self.output.getvalue())
        self.assertEqual(created, [])

    def test_labels_offered_for_a_replaced_repository_or_without_saving_are_not_created(self):
        created = []

        def command(*args):
            if args[:3] == ("gh", "label", "create"):
                created.append(args[3:6])
                return ""
            return self.responses.get(args)

        self.command = command
        self.responses[labels_of("acme/other")] = json.dumps([[]])
        self.run_configure(answers=["5", "1", "acme/other", "", "", "7"])  # Yes, then Quit.
        self.assertEqual(created, [])
        # Yes for acme/other, then acme/full has them all: only the last repository counts.
        self.responses[labels_of("acme/full")] = json.dumps([[{"name": name} for name in init.LABELS.values()]])
        self.run_configure(answers=["5", "1", "acme/other", "", "1", "acme/full", "", "6"])
        self.assertEqual(created, [])
        self.assertEqual(load_config(self.root / "ralph.toml", self.root).repository, "acme/full")

    def test_going_back_to_the_saved_repository_offers_no_labels(self):
        created = []

        def command(*args):
            if args[:3] == ("gh", "label", "create"):
                created.append(args[3:6])
                return ""
            return self.responses.get(args)

        self.command = command
        self.responses[labels_of("acme/other")] = json.dumps([[]])
        self.responses[labels_of("acme/widget")] = json.dumps([[]])
        # Yes for acme/other, back to acme/widget, then a remote change so Save writes.
        self.run_configure(answers=["5", "1", "acme/other", "", "1", "acme/widget", "3", "upstream", "", "6"])
        self.assertNotIn("Labels missing on acme/widget", self.output.getvalue())
        self.assertEqual(created, [])
        self.assertEqual(load_config(self.root / "ralph.toml", self.root).remote, "upstream")


if __name__ == "__main__":
    unittest.main()
