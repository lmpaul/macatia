"""`ralph-kit configure` without real git, gh, or terminal."""

import contextlib
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

from ralph_kit import configure, init, update_config
from ralph_kit.config import load_config


sys.path.insert(0, str(KIT_ROOT / "tests"))
from test_init import CUSTOM_CONFIGURATION, DEFAULT_CONFIGURATION  # noqa: E402


GH_LOGIN = ("gh", "api", "user", "--jq", ".login")
GH_REPOSITORY = ("gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef")
GIT_REMOTES = ("git", "remote")
CODEX_MODELS = ("codex", "debug", "models")
CODEX_CATALOG = json.dumps({"models": [
    {"slug": "codex-large", "description": "Smart", "visibility": "list", "priority": 2,
     "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "high"}, {"effort": "ultra"}]},
    {"slug": "codex-mini", "description": "Mini", "visibility": "list", "priority": 5,
     "supported_reasoning_levels": [{"effort": "low"}]},
]})


BACKUP_SECTION = """
# Takes over every session for the rest of a run after an engine failure.
[models.backup]
engine = "codex"
name = "codex-large"
display_name = "codex-large"
reasoning_effort = "medium"
"""
WITHOUT_BACKUP = DEFAULT_CONFIGURATION.replace(BACKUP_SECTION, "")
_start, _review = CUSTOM_CONFIGURATION.split("# Used by reviewed runs", 1)
WITHOUT_REVIEW = _start + _review.split("\n\n", 1)[1]
REVIEWED_BY_DEFAULT = DEFAULT_CONFIGURATION.replace("review_by_default = false\n", "review_by_default = true\n")
SLEEP_LOCK_BY_DEFAULT = DEFAULT_CONFIGURATION.replace("caffeinate_by_default = false\n", "caffeinate_by_default = true\n")
WORKTREE_SETUP = DEFAULT_CONFIGURATION.replace(
    "caffeinate_by_default = false\n",
    "caffeinate_by_default = false\n"
    "# Prepares a run worktree: ralph-kit run --worktree runs it through the shell inside the run worktree.\n"
    'worktree_setup = "uv sync --frozen"\n',
)


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
        self.installed = set()  # Agent CLIs on PATH.
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
            self.root, prompter, command=lambda *args: self.command(*args),
            which=lambda name: f"/usr/bin/{name}" if name in self.installed else None, **options
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

    def test_an_invalid_configuration_is_refused_with_a_fix_by_hand_before_starting_over(self):
        for config, suggestion in (
            (None, "Fix ralph.toml by hand: `ralph-kit init --force` would start over and lose its settings."),
            (pathlib.Path("b.toml"), "Fix b.toml by hand: `ralph-kit init --config b.toml --force` would start over and lose its settings."),
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
        self.run_configure(answers=["11"])
        menu = self.menu()
        self.assertIn("? ralph.toml — what do you want to change?", self.output.getvalue())
        lines = [line.strip() for line in menu.strip().splitlines()]
        self.assertEqual(lines[0], "1)   Model profiles  (implementation claude/sonnet medium · review claude/opus high · correction = implementation)")
        self.assertTrue(lines[1].startswith("2)   Backup  (codex/codex-large medium)"))
        self.assertTrue(lines[2].startswith("3)   Trusted authors  (octocat)"))
        self.assertEqual(lines[3], "4)   Verification  (local off · CI off)")
        self.assertEqual(lines[4], "5)   Git  (acme/widget · trunk · origin)")
        self.assertEqual(lines[5], "6)   Reviewed runs by default  (off)")
        self.assertEqual(lines[6], "7)   Keep the machine awake by default  (off)")
        self.assertEqual(lines[7], "8)   Child session limits  (standard 5 · reviewed 9)")
        self.assertEqual(lines[8:], ["9)   Run worktree setup  (none)", "10)   Save", "11)   Quit without saving"])

    def test_quit_without_saving_writes_nothing(self):
        written = self.run_configure(answers=["4", "1", "2", "", "11"])
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
        self.assertTrue(self.run_configure(answers=["4", "2", "", "10"]))
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
        self.run_configure(answers=["4", "1", "", "10"])
        self.assertNotIn("⚠", self.output.getvalue())
        self.assertTrue(load_config(self.root / "ralph.toml", self.root).verification.local_checks)

    def test_a_save_keeps_every_value_it_did_not_change(self):
        # Custom labels, quality policy paths, display names, a distinct correction, several authors, another remote.
        self.write(CUSTOM_CONFIGURATION)
        self.run_configure(answers=["4", "1", "", "10"])
        self.assertEqual(self.read(), CUSTOM_CONFIGURATION.replace("local_checks = true", "local_checks = false"))
        self.assertIn("Changes:\n  verification.local_checks: true → false\n\n", self.output.getvalue())
        self.assertNotIn("⚠", self.output.getvalue())  # Turning a check off needs no quality policy.

    def test_a_configuration_without_review_stays_without_it(self):
        self.write(WITHOUT_REVIEW)
        self.run_configure(answers=["4", "2", "", "10"])
        self.assertEqual(self.read(), WITHOUT_REVIEW.replace("ci_checks = false", "ci_checks = true"))

    def test_comments_the_renderer_would_not_write_are_announced_once(self):
        self.write("# Tuned by hand\n" + DEFAULT_CONFIGURATION.replace('remote = "origin"', 'remote = "origin"  # fork'))
        self.run_configure(answers=["4", "1", "", "10"])
        output = self.output.getvalue()
        self.assertEqual(output.count("has comments that ralph-kit does not write: saving will drop them"), 1)
        self.assertNotIn("Tuned by hand", self.read())

    def test_rendered_comments_and_hashes_in_strings_are_not_announced(self):
        self.write(DEFAULT_CONFIGURATION.replace('"needs-triage"', '"needs #triage"'))
        self.run_configure(answers=["11"])
        self.assertNotIn("comments", self.output.getvalue())

    def test_defaulted_settings_are_announced_once_with_update_config(self):
        for config, suggestion in ((None, "`ralph-kit update-config`"), (pathlib.Path("b.toml"), "`ralph-kit update-config --config b.toml`")):
            with self.subTest(config=config):
                self.output = io.StringIO()
                name = str(config or "ralph.toml")
                self.write(
                    DEFAULT_CONFIGURATION.replace("review_by_default = false\n", "")
                    .replace('standalone = "standalone"\n', "")
                    .replace("reviewed = 9\n", ""),
                    name=name,
                )
                self.run_configure(answers=["11"], config=config)
                lines = [line for line in self.output.getvalue().splitlines() if "leaves out" in line]
                self.assertEqual(lines, [
                    f"ℹ {name} leaves out child_session_limit.reviewed, labels.standalone, review_by_default, "
                    f"so their default values are used: {suggestion} writes them."
                ])

    def test_one_defaulted_setting_is_announced_in_the_singular(self):
        self.write(DEFAULT_CONFIGURATION.replace("caffeinate_by_default = false\n", ""))
        self.run_configure(answers=["11"])
        self.assertIn(
            "ℹ ralph.toml leaves out caffeinate_by_default, so its default value is used: "
            "`ralph-kit update-config` writes it.",
            self.output.getvalue(),
        )

    def test_a_file_declaring_every_setting_announces_no_defaulted_setting(self):
        self.run_configure(answers=["11"])
        self.assertNotIn("leaves out", self.output.getvalue())

    def test_a_file_completed_by_update_config_opens_without_announcements(self):
        review = "# true makes every run a reviewed run; ralph-kit run --no-review overrides it.\nreview_by_default = false\n"
        self.write(
            DEFAULT_CONFIGURATION.replace(review, "")
            .replace('work_started = "work-started"\nstandalone = "standalone"\n', "")
            .split("\n[child_session_limit]\n", 1)[0]
        )
        labels = json.dumps([[{"name": name} for name in init.DEFAULT_LABELS.values()]])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(update_config.run(self.root, self.root / "ralph.toml", command=lambda *args: labels), 0)

        self.assertFalse(self.run_configure(answers=["10"]))  # Save without a change.
        output = self.output.getvalue()
        self.assertNotIn("leaves out", output)
        self.assertNotIn("comments", output)
        self.assertIn("No changes: nothing written.", output)

    def test_trusted_authors_lists_them_and_adds_one_without_duplicates(self):
        self.write(CUSTOM_CONFIGURATION)
        # Add zoe, then alice again, then Back and Save.
        self.assertTrue(self.run_configure(answers=["3", "1", "zoe smith", "zoe", "1", "alice", "", "10"]))
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
        self.assertFalse(self.run_configure(answers=["3", "1", "", "", "10"]))
        self.assertIn("GitHub account to trust (leave empty to go back)", self.output.getvalue())
        self.assertIn("No changes: nothing written.", self.output.getvalue())

    def test_trusted_authors_removes_one(self):
        self.write(CUSTOM_CONFIGURATION)
        # Remove bob (second of alice, bob, octocat), then Back and Save.
        self.assertTrue(self.run_configure(answers=["3", "2", "2", "", "10"]))
        self.assertIn("  ✓ bob removed", self.output.getvalue())
        self.assertEqual(load_config(self.root / "ralph.toml", self.root).trusted_authors, {"alice", "octocat"})
        self.assertEqual(self.read(), CUSTOM_CONFIGURATION.replace('["alice", "bob", "octocat"]', '["alice", "octocat"]'))

    def test_removing_the_last_trusted_author_is_refused(self):
        self.assertFalse(self.run_configure(answers=["3", "2", "", "10"]))
        output = self.output.getvalue()
        self.assertIn("  ✗ octocat is the last trusted author: add another one before removing it.", output)
        self.assertIn("No changes: nothing written.", output)
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION)

    def test_a_gh_login_that_is_not_trusted_is_warned_about(self):
        warning = "  ⚠ The current gh account, hubot, is not a trusted author: Ralph would ignore its own implementation evidence."
        self.responses[GH_LOGIN] = "hubot"
        self.run_configure(answers=["3", "1", "hubot", "", "11"])
        output = self.output.getvalue()
        self.assertEqual(output.count(warning), 1)  # Not after hubot was added.
        self.assertLess(output.index(warning), output.index("? Trusted authors: octocat —"))

    def test_removing_the_gh_login_warns(self):
        self.write(CUSTOM_CONFIGURATION)
        self.responses[GH_LOGIN] = "octocat"
        self.run_configure(answers=["3", "2", "3", "", "11"])
        self.assertIn("  ✓ octocat removed\n  ⚠ The current gh account, octocat, is not a trusted author", self.output.getvalue())

    def test_no_warning_when_the_gh_login_is_trusted_or_gh_is_missing(self):
        for login in ("octocat", None):
            with self.subTest(login=login):
                self.output = io.StringIO()
                self.responses[GH_LOGIN] = login
                self.run_configure(answers=["3", "", "11"])
                self.assertNotIn("⚠", self.output.getvalue())

    def test_config_option_edits_another_configuration(self):
        self.write(DEFAULT_CONFIGURATION, name="b.toml")
        self.run_configure(answers=["4", "1", "", "10"], config=pathlib.Path("b.toml"))
        self.assertIn("? b.toml — what do you want to change?", self.output.getvalue())
        self.assertTrue(load_config(self.root / "b.toml", self.root).verification.local_checks)
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION)
        self.assertIn("Next step: commit b.toml, then run `ralph-kit check --config b.toml`.", self.output.getvalue())

    def test_git_edits_the_repository_base_branch_and_remote(self):
        # Without gh or git output: no hint and no label offer, editing still works.
        answers = ["5", "1", "acme", "acme/gadget", "2", "-x", "develop", "3", "my fork", "upstream", "", "10"]
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
        self.assertFalse(self.run_configure(answers=["5", "1", "", "2", "", "3", "", "", "10"]))
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
        self.run_configure(answers=["5", "1", "", "2", "", "3", "", "", "11"])
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
        self.run_configure(answers=["5", "", "11"])
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
                created.append((args[3], args[args.index("--color") + 1]))
                return None if args[3] == "done" else ""
            return self.responses.get(args)

        self.command = command
        self.assertTrue(self.run_configure(answers=["5", "1", "acme/other", "", "", "10"]))  # Enter keeps "Yes".
        output = self.output.getvalue()
        self.assertIn("  ℹ Labels missing on acme/other: agent: standalone, agent: started, agent: working, done, human: ready, human: review, triage: info, won't fix", output)
        self.assertIn("? Create the 8 missing labels?", output)
        # Each renamed label carries its role's colour.
        self.assertEqual(created, [
            ("triage: info", "F9D0C4"), ("human: ready", "1D76DB"), ("won't fix", "CFD3D7"), ("agent: working", "5319E7"),
            ("human: review", "D93F0B"), ("done", "C2E0C6"), ("agent: started", "E4E9F2"), ("agent: standalone", "E4E9F2"),
        ])
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
        self.run_configure(answers=["5", "1", "acme/other", "2", "", "10"])
        self.assertIn("? Create the 10 missing labels?", self.output.getvalue())
        self.run_configure(answers=["5", "1", "acme/full", "", "10"])
        self.assertIn("  ✓ Labels: all 10 present", self.output.getvalue())
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
        self.run_configure(answers=["5", "1", "acme/other", "", "", "11"])  # Yes, then Quit.
        self.assertEqual(created, [])
        # Yes for acme/other, then acme/full has them all: only the last repository counts.
        self.responses[labels_of("acme/full")] = json.dumps([[{"name": name} for name in init.LABELS.values()]])
        self.run_configure(answers=["5", "1", "acme/other", "", "1", "acme/full", "", "10"])
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
        self.run_configure(answers=["5", "1", "acme/other", "", "1", "acme/widget", "3", "upstream", "", "10"])
        self.assertNotIn("Labels missing on acme/widget", self.output.getvalue())
        self.assertEqual(created, [])
        self.assertEqual(load_config(self.root / "ralph.toml", self.root).remote, "upstream")

    def models(self):
        config = load_config(self.root / "ralph.toml", self.root, require_review=False)
        return {
            role: (profile.engine, profile.name, profile.display_name, profile.reasoning_effort)
            for role, profile in config.models.items()
        }

    def test_model_profiles_changes_the_engine_model_and_effort_of_a_role(self):
        self.installed = {"claude", "codex"}
        self.responses[CODEX_MODELS] = CODEX_CATALOG
        # Review: codex, first listed model, current effort kept; then Back and Save.
        self.assertTrue(self.run_configure(answers=["1", "2", "2", "", "", "", "10"]))
        output = self.output.getvalue()
        self.assertIn(
            "? Model profiles: which session role do you want to change?\n"
            "  1) implementation  (claude/sonnet medium)\n  2) review  (claude/opus high)\n"
            "  3) correction  (same as implementation)\n  4) Back\n",
            output,
        )
        self.assertIn("? Engine for review\n  1) claude  (Claude Code)\n  2) codex  (Codex)\n", output)
        self.assertIn("Choice [1]: ", self.prompts)  # The current engine is the default.
        self.assertIn("  1) codex-large  (Smart)\n  2) codex-mini  (Mini)\n  3) other", output)
        self.assertIn("  1) medium  (unavailable: same as backup)\n  2) high\n  3) ultra\n", output)
        self.assertIn("  ✓ review: codex/codex-large high", output)
        self.assertIn("* Model profiles  (implementation claude/sonnet medium · review codex/codex-large high", self.menu())
        self.assertIn(
            "Changes:\n  models.review.engine: claude → codex\n  models.review.name: opus → codex-large\n"
            "  models.review.display_name: opus → codex-large\n",
            output,
        )
        self.assertEqual(self.models()["review"], ("codex", "codex-large", "codex-large", "high"))
        self.assertEqual(
            self.read(),
            DEFAULT_CONFIGURATION.replace(
                'engine = "claude"\nname = "opus"\ndisplay_name = "opus"',
                'engine = "codex"\nname = "codex-large"\ndisplay_name = "codex-large"',
            ),
        )

    def test_the_current_engine_is_offered_even_when_not_installed(self):
        self.installed = {"codex"}
        self.run_configure(answers=["1", "1", "", "", "", "", "11"])
        self.assertIn("? Engine for implementation\n  1) claude  (Claude Code, not installed here)\n  2) codex  (Codex)\n", self.output.getvalue())
        # With no other CLI installed, the current engine is kept without asking.
        self.output = io.StringIO()
        self.installed = set()
        self.assertFalse(self.run_configure(answers=["1", "1", "", "", "", "10"]))
        output = self.output.getvalue()
        self.assertNotIn("? Engine for", output)
        self.assertIn("  Engine: claude (Claude Code, no other agent CLI installed)", output)
        self.assertIn("No changes: nothing written.", output)

    def test_correction_follows_implementation_when_identical_and_on_yes(self):
        # Implementation: fable, current effort medium kept; Enter answers Yes; Back, Save.
        self.assertTrue(self.run_configure(answers=["1", "1", "1", "", "", "", "10"]))
        output = self.output.getvalue()
        self.assertIn("  1) fable  (Fable, for the hardest and longest tasks)", output)
        self.assertIn("? Correction has the same model profile as implementation: should it follow?", output)
        self.assertIn("  3) correction  (same as implementation)", output.rsplit("Model profiles: which", 1)[1])
        self.assertEqual(self.models()["correction"], ("claude", "fable", "fable", "medium"))
        self.assertEqual(self.models()["correction"], self.models()["implementation"])
        self.assertIn("models.correction.name: sonnet → fable", output)

    def test_correction_keeps_its_profile_on_no(self):
        self.assertTrue(self.run_configure(answers=["1", "1", "1", "", "2", "", "10"]))
        self.assertEqual(self.models()["implementation"], ("claude", "fable", "fable", "medium"))
        self.assertEqual(self.models()["correction"], ("claude", "sonnet", "sonnet", "medium"))
        self.assertIn("· correction claude/sonnet medium", self.menu())

    def test_a_distinct_correction_is_not_asked_to_follow(self):
        self.write(CUSTOM_CONFIGURATION)
        self.assertTrue(self.run_configure(answers=["1", "1", "4", "", "", "10"]))  # Implementation: haiku.
        self.assertNotIn("should it follow", self.output.getvalue())
        self.assertEqual(self.models()["implementation"], ("claude", "haiku", "haiku", "medium"))
        self.assertEqual(self.models()["correction"], ("claude", "opus", "Opus fixer", "high"))

    def test_correction_is_edited_on_its_own(self):
        self.assertTrue(self.run_configure(answers=["1", "3", "2", "3", "", "10"]))  # Opus, high.
        self.assertEqual(self.models()["correction"], ("claude", "opus", "opus", "high"))
        self.assertEqual(self.models()["implementation"], ("claude", "sonnet", "sonnet", "medium"))
        self.assertIn("· correction claude/opus high", self.menu())

    def test_an_unchanged_model_keeps_its_display_name(self):
        self.write(CUSTOM_CONFIGURATION)
        # Implementation: sonnet kept, effort changed to high.
        self.assertTrue(self.run_configure(answers=["1", "1", "", "3", "", "10"]))
        self.assertEqual(self.models()["implementation"], ("claude", "sonnet", 'Sonnet "daily"', "high"))
        self.assertIn("Changes:\n  models.implementation.reasoning_effort: medium → high\n\n", self.output.getvalue())

    def test_efforts_same_as_the_backup_are_unavailable_and_refused_by_number(self):
        self.installed = {"claude", "codex"}
        self.responses[CODEX_MODELS] = CODEX_CATALOG
        # Implementation: codex, codex-large, medium refused, then high; correction follows.
        self.assertTrue(self.run_configure(answers=["1", "1", "2", "1", "1", "2", "", "", "10"]))
        output = self.output.getvalue()
        self.assertIn("  medium is unavailable: same as backup.", output)
        self.assertEqual(self.models()["implementation"], ("codex", "codex-large", "codex-large", "high"))
        self.assertEqual(self.models()["correction"], self.models()["implementation"])

    def test_a_model_is_unavailable_when_every_effort_is_the_backups(self):
        self.write(DEFAULT_CONFIGURATION.replace('name = "codex-large"\ndisplay_name = "codex-large"\nreasoning_effort = "medium"',
                                                 'name = "codex-mini"\ndisplay_name = "codex-mini"\nreasoning_effort = "low"'))
        self.installed = {"claude", "codex"}
        self.responses[CODEX_MODELS] = CODEX_CATALOG
        # Review: codex, codex-mini refused, other with codex-mini (explained, asked again), then codex-large.
        answers = ["1", "2", "2", "2", "3", "codex-mini", "1", "", "", "10"]
        self.assertTrue(self.run_configure(answers=answers))
        output = self.output.getvalue()
        self.assertIn("  2) codex-mini  (Mini)  (unavailable: every reasoning effort same as backup)", output)
        self.assertEqual(output.count("  codex-mini is unavailable: every reasoning effort same as backup."), 2)
        self.assertEqual(self.models()["review"], ("codex", "codex-large", "codex-large", "high"))

    def test_without_the_codex_catalog_the_current_model_is_the_default(self):
        self.write(CUSTOM_CONFIGURATION)
        # Review stays codex (not installed here, the only engine), Enter keeps gpt-5 and xhigh.
        self.assertFalse(self.run_configure(answers=["1", "2", "", "", "", "10"]))
        output = self.output.getvalue()
        self.assertIn("? Model for review (Codex model list unavailable)", output)
        self.assertIn("Answer [gpt-5]: ", self.prompts)
        self.assertIn("No changes: nothing written.", output)
        self.assertEqual(self.read(), CUSTOM_CONFIGURATION)

    def test_a_current_model_missing_from_the_catalog_stays_offered(self):
        self.write(CUSTOM_CONFIGURATION)
        self.responses[CODEX_MODELS] = CODEX_CATALOG
        self.assertFalse(self.run_configure(answers=["1", "2", "", "", "", "10"]))
        self.assertIn("  1) gpt-5  (current model)\n  2) codex-large  (Smart)\n", self.output.getvalue())
        self.assertEqual(self.read(), CUSTOM_CONFIGURATION)

    def test_a_configuration_without_review_can_add_one(self):
        self.write(WITHOUT_REVIEW)
        self.assertTrue(self.run_configure(answers=["1", "2", "", "", "", "10"]))  # Init's defaults: opus, high.
        self.assertIn("  2) review  (none)", self.output.getvalue())
        self.assertEqual(self.models()["review"], ("claude", "opus", "opus", "high"))
        self.assertEqual(self.read(), CUSTOM_CONFIGURATION.replace(
            'engine = "codex"\nname = "gpt-5"\ndisplay_name = "GPT-5 review"\nreasoning_effort = "xhigh"',
            'engine = "claude"\nname = "opus"\ndisplay_name = "opus"\nreasoning_effort = "high"',
        ))

    def test_turning_reviewed_runs_by_default_on_saves_only_the_setting(self):
        self.assertTrue(self.run_configure(answers=["6", "1", "10"]))
        output = self.output.getvalue()
        self.assertIn(
            "? Reviewed runs by default: off — what do you want to do?\n"
            "  1) Turn on  (every run is a reviewed run unless started with --no-review)\n  2) Back\n",
            output,
        )
        self.assertNotIn("Model for review", output)
        self.assertIn("* Reviewed runs by default  (on)", self.menu())
        self.assertIn("Changes:\n  review_by_default: false → true\n\n", output)
        self.assertEqual(self.read(), REVIEWED_BY_DEFAULT)
        self.assertTrue(load_config(self.root / "ralph.toml", self.root).review_by_default)

    def test_turning_reviewed_runs_by_default_on_without_review_asks_for_its_profile(self):
        self.write(WITHOUT_REVIEW)
        self.assertTrue(self.run_configure(answers=["6", "1", "", "", "10"]))  # Init's defaults: opus, high.
        output = self.output.getvalue()
        self.assertIn("? Model for review\n", output)
        self.assertIn("  ✓ review: claude/opus high", output)
        self.assertIn("* Model profiles", self.menu())
        self.assertIn("* Reviewed runs by default  (on)", self.menu())
        config = load_config(self.root / "ralph.toml", self.root, require_review=True)
        self.assertTrue(config.review_by_default)
        self.assertEqual(self.models()["review"], ("claude", "opus", "opus", "high"))

    def test_turning_reviewed_runs_by_default_off_keeps_the_review_profile(self):
        self.write(REVIEWED_BY_DEFAULT)
        self.assertTrue(self.run_configure(answers=["6", "1", "10"]))
        output = self.output.getvalue()
        self.assertIn("  6)   Reviewed runs by default  (on)", output)
        self.assertIn("  1) Turn off  (runs are standard unless started with --review)\n", output)
        self.assertNotIn("comments that ralph-kit does not write", output)
        self.assertIn("Changes:\n  review_by_default: true → false\n\n", output)
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION)
        self.assertIn("review", load_config(self.root / "ralph.toml", self.root).models)

    def test_going_back_from_reviewed_runs_by_default_changes_nothing(self):
        self.assertFalse(self.run_configure(answers=["6", "2", "10"]))
        self.assertIn("No changes: nothing written.", self.output.getvalue())
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION)

    def test_turning_sleep_lock_by_default_on_saves_only_the_setting(self):
        self.assertTrue(self.run_configure(answers=["7", "1", "10"]))
        output = self.output.getvalue()
        self.assertIn(
            "? Keep the machine awake by default: off — what do you want to do?\n"
            "  1) Turn on  (every run holds a sleep lock unless started with --no-caffeinate)\n  2) Back\n",
            output,
        )
        self.assertIn("* Keep the machine awake by default  (on)", self.menu())
        self.assertIn("Changes:\n  caffeinate_by_default: false → true\n\n", output)
        self.assertEqual(self.read(), SLEEP_LOCK_BY_DEFAULT)
        self.assertTrue(load_config(self.root / "ralph.toml", self.root).caffeinate_by_default)

    def test_turning_sleep_lock_by_default_off_saves_only_the_setting(self):
        self.write(SLEEP_LOCK_BY_DEFAULT)
        self.assertTrue(self.run_configure(answers=["7", "1", "10"]))
        output = self.output.getvalue()
        self.assertIn("  7)   Keep the machine awake by default  (on)", output)
        self.assertIn("  1) Turn off  (runs hold no sleep lock unless started with --caffeinate)\n", output)
        self.assertIn("Changes:\n  caffeinate_by_default: true → false\n\n", output)
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION)
        self.assertFalse(load_config(self.root / "ralph.toml", self.root).caffeinate_by_default)

    def test_going_back_from_sleep_lock_by_default_changes_nothing(self):
        self.assertFalse(self.run_configure(answers=["7", "2", "10"]))
        self.assertIn("No changes: nothing written.", self.output.getvalue())
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION)

    def test_child_session_limits_change_with_the_loading_validation_and_save_only_what_changed(self):
        self.assertTrue(self.run_configure(answers=["8", "1", "three", "5_0", "0", "3", "2", "1", "", "", "10"]))
        output = self.output.getvalue()
        self.assertIn(
            "? Child session limits: which limit do you want to change?\n"
            "  1) Standard runs  (5 agent sessions per child)\n"
            "  2) Reviewed runs  (9 agent sessions per child)\n  3) Back\n",
            output,
        )
        self.assertEqual(output.count("  must be an integer\n"), 2)
        self.assertIn("  must be at least 1\n", output)
        self.assertIn("  must be at least 2\n", output)  # Reviewed: refused 1, then kept 9 on Enter.
        self.assertIn("  ✓ Standard runs: 3 agent sessions per child\n", output)
        self.assertIn("* Child session limits  (standard 3 · reviewed 9)", self.menu())
        self.assertIn("Changes:\n  child_session_limit.standard: 5 → 3\n\n", output)
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION.replace("standard = 5\n", "standard = 3\n"))
        limits = load_config(self.root / "ralph.toml", self.root).child_session_limits
        self.assertEqual((limits.standard, limits.reviewed), (3, 9))

    def test_a_configuration_without_child_session_limits_saves_their_defaults_only_with_a_change(self):
        start = DEFAULT_CONFIGURATION.split("\n[child_session_limit]", 1)[0]
        self.write(start)
        self.assertFalse(self.run_configure(answers=["8", "2", "", "3", "10"]))  # Enter keeps 9.
        self.assertIn("No changes: nothing written.", self.output.getvalue())
        self.assertEqual(self.read(), start)
        self.assertTrue(self.run_configure(answers=["8", "2", "12", "3", "10"]))
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION.replace("reviewed = 9\n", "reviewed = 12\n"))

    def test_run_worktree_setup_is_set_and_saved_after_the_top_level_settings(self):
        self.assertTrue(self.run_configure(answers=["9", "1", "uv sync --frozen", "3", "10"]))
        output = self.output.getvalue()
        self.assertIn(
            "? Run worktree setup: none — what do you want to do?\n"
            "  1) Set the command  (the shell command that prepares a run worktree for ralph-kit run --worktree)\n"
            "  2) Back\n",
            output,
        )
        self.assertIn("  ✓ run worktree setup: uv sync --frozen\n", output)
        self.assertIn("* Run worktree setup  (uv sync --frozen)", self.menu())
        self.assertIn("Changes:\n  worktree_setup: (none) → uv sync --frozen\n\n", output)
        self.assertEqual(self.read(), WORKTREE_SETUP)
        self.assertEqual(load_config(self.root / "ralph.toml", self.root).worktree_setup, "uv sync --frozen")

    def test_setting_no_run_worktree_setup_changes_nothing(self):
        self.assertFalse(self.run_configure(answers=["9", "1", "", "2", "10"]))
        self.assertIn("No changes: nothing written.", self.output.getvalue())
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION)

    def test_run_worktree_setup_is_changed_from_its_current_value(self):
        self.write(WORKTREE_SETUP)
        self.assertTrue(self.run_configure(answers=["9", "1", "pnpm install --frozen-lockfile", "3", "10"]))
        output = self.output.getvalue()
        self.assertIn("  9)   Run worktree setup  (uv sync --frozen)", output)
        self.assertIn(
            "? Run worktree setup: uv sync --frozen — what do you want to do?\n"
            "  1) Change the command\n  2) Remove the command\n  3) Back\n",
            output,
        )
        self.assertIn("Answer [uv sync --frozen]: ", self.prompts)
        self.assertIn("Changes:\n  worktree_setup: uv sync --frozen → pnpm install --frozen-lockfile\n\n", output)
        self.assertEqual(load_config(self.root / "ralph.toml", self.root).worktree_setup, "pnpm install --frozen-lockfile")

    def test_keeping_the_current_run_worktree_setup_changes_nothing(self):
        self.write(WORKTREE_SETUP)
        self.assertFalse(self.run_configure(answers=["9", "1", "", "3", "10"]))  # Enter keeps it.
        self.assertIn("No changes: nothing written.", self.output.getvalue())
        self.assertEqual(self.read(), WORKTREE_SETUP)

    def test_run_worktree_setup_is_removed(self):
        self.write(WORKTREE_SETUP)
        self.assertTrue(self.run_configure(answers=["9", "2", "2", "10"]))
        output = self.output.getvalue()
        self.assertIn("  ✓ run worktree setup removed\n", output)
        self.assertIn("* Run worktree setup  (none)", self.menu())
        self.assertIn("Changes:\n  worktree_setup: uv sync --frozen → (none)\n\n", output)
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION)
        self.assertIsNone(load_config(self.root / "ralph.toml", self.root).worktree_setup)

    def backup(self):
        backup = load_config(self.root / "ralph.toml", self.root).backup
        return backup and (backup.engine, backup.name, backup.display_name, backup.reasoning_effort)

    def test_without_a_backup_the_section_adds_one_on_another_installed_cli(self):
        self.write(WITHOUT_BACKUP)
        self.installed = {"claude", "codex"}
        self.responses[CODEX_MODELS] = CODEX_CATALOG
        # Add, Enter keeps codex, codex-large and medium; Back, Save.
        self.assertTrue(self.run_configure(answers=["2", "1", "", "", "", "", "10"]))
        output = self.output.getvalue()
        self.assertIn("  2)   Backup  (none)", output)
        self.assertIn("? Backup: none — what do you want to do?\n  1) Add a backup\n  2) Back\n", output)
        self.assertIn(
            "? Engine for the backup\n  1) codex  (Codex, recommended: a different CLI)\n  2) claude  (Claude Code)\n", output
        )
        self.assertIn("  ✓ backup: codex/codex-large medium", output)
        self.assertIn("? Backup: codex/codex-large medium — what do you want to do?\n  1) Change the backup\n  2) Remove the backup\n", output)
        self.assertIn("* Backup  (codex/codex-large medium)", self.menu())
        self.assertIn("Changes:\n  models.backup.engine: (none) → codex\n  models.backup.name: (none) → codex-large\n", output)
        self.assertEqual(self.read(), DEFAULT_CONFIGURATION)

    def test_the_backup_changes_with_its_current_values_as_defaults(self):
        self.installed = {"claude", "codex"}
        self.responses[CODEX_MODELS] = CODEX_CATALOG
        # Change, Enter keeps codex and codex-large, effort high; Back, Save.
        self.assertTrue(self.run_configure(answers=["2", "1", "", "", "2", "", "10"]))
        self.assertIn("  1) codex-large  (Smart)\n  2) codex-mini  (Mini)\n  3) other", self.output.getvalue())
        self.assertIn("Changes:\n  models.backup.reasoning_effort: medium → high\n\n", self.output.getvalue())
        self.assertEqual(self.backup(), ("codex", "codex-large", "codex-large", "high"))

    def test_an_unchanged_backup_keeps_its_display_name_and_writes_nothing(self):
        self.write(CUSTOM_CONFIGURATION)
        self.installed = {"codex"}
        self.assertFalse(self.run_configure(answers=["2", "1", "", "", "", "10"]))  # Engine only codex: kept.
        output = self.output.getvalue()
        self.assertIn("  Engine: codex (Codex, no other agent CLI installed)", output)
        self.assertIn("Answer [gpt-5-mini]: ", self.prompts)
        self.assertIn("No changes: nothing written.", output)
        self.assertEqual(self.read(), CUSTOM_CONFIGURATION)

    def test_removing_the_backup_drops_it_from_the_file(self):
        self.assertTrue(self.run_configure(answers=["2", "2", "", "10"]))
        output = self.output.getvalue()
        self.assertIn("  ✓ backup removed", output)
        self.assertIn("? Backup: none — what do you want to do?", output)
        self.assertIn("* Backup  (none)", self.menu())
        self.assertIn("Changes:\n  models.backup.engine: codex → (none)\n", output)
        self.assertIsNone(self.backup())
        self.assertEqual(self.read(), WITHOUT_BACKUP)

    def test_backup_efforts_same_as_a_session_role_are_unavailable(self):
        self.installed = {"claude"}
        # Change: claude (codex stays offered, not installed), opus, high refused, then Enter keeps medium.
        self.assertTrue(self.run_configure(answers=["2", "1", "1", "2", "3", "", "", "10"]))
        output = self.output.getvalue()
        self.assertIn("? Engine for the backup\n  1) claude  (Claude Code)\n  2) codex  (Codex, not installed here)\n", output)
        self.assertIn("Choice [2]: ", self.prompts)  # The current engine is the default.
        self.assertIn("  3) high  (unavailable: same as review)\n", output)
        self.assertIn("  high is unavailable: same as review.", output)
        self.assertEqual(self.backup(), ("claude", "opus", "opus", "medium"))

    def test_a_backup_model_whose_every_effort_clashes_is_unavailable(self):
        self.write(DEFAULT_CONFIGURATION.replace(
            'engine = "claude"\nname = "opus"\ndisplay_name = "opus"\nreasoning_effort = "high"',
            'engine = "codex"\nname = "codex-mini"\ndisplay_name = "codex-mini"\nreasoning_effort = "low"',
        ))
        self.installed = {"codex"}
        self.responses[CODEX_MODELS] = CODEX_CATALOG
        # Change, codex-mini refused, other with codex-mini (explained, asked again), then codex-large and ultra.
        self.assertTrue(self.run_configure(answers=["2", "1", "2", "3", "codex-mini", "1", "3", "", "10"]))
        output = self.output.getvalue()
        self.assertIn("  2) codex-mini  (Mini)  (unavailable: every reasoning effort same as review)", output)
        self.assertEqual(output.count("  codex-mini is unavailable: every reasoning effort same as review."), 2)
        self.assertEqual(self.backup(), ("codex", "codex-large", "codex-large", "ultra"))

    def test_a_backup_is_added_without_any_agent_cli_or_codex_catalog(self):
        self.write(WITHOUT_BACKUP)
        # Add, codex (every engine offered), a typed model, Enter keeps medium; Back, Save.
        self.assertTrue(self.run_configure(answers=["2", "1", "2", "gpt-5", "", "", "10"]))
        output = self.output.getvalue()
        self.assertIn(
            "? Engine for the backup\n  1) claude  (Claude Code, not installed here)\n  2) codex  (Codex, not installed here)\n",
            output,
        )
        self.assertIn("? Model for the backup (Codex model list unavailable)", output)
        self.assertEqual(self.backup(), ("codex", "gpt-5", "gpt-5", "medium"))

    def test_without_any_agent_cli_the_backup_can_still_change_engine(self):
        # Change: claude offered though nothing is installed, opus, Enter keeps medium; Back, Save.
        self.assertTrue(self.run_configure(answers=["2", "1", "1", "2", "", "", "10"]))
        self.assertIn(
            "? Engine for the backup\n  1) claude  (Claude Code, not installed here)\n  2) codex  (Codex, not installed here)\n",
            self.output.getvalue(),
        )
        self.assertIn("Choice [2]: ", self.prompts)
        self.assertEqual(self.backup(), ("claude", "opus", "opus", "medium"))


if __name__ == "__main__":
    unittest.main()
