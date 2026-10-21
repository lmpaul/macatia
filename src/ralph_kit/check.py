"""Readiness checks; never run agents or project checks. The only GitHub write is creating missing labels, once confirmed."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass

from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from . import __version__, keep_awake
from .agent import engine_help_command_and_flags
from .config import (
    DIAGNOSTICS_DIRECTORY,
    MissingConfigError,
    RalphConfigError,
    load_config,
)
from .progress import spinner


PACKAGE_DIRECTORY = pathlib.Path(__file__).resolve().parent
AGENT_CLIS = {"claude": "Claude Code", "codex": "Codex"}
INSTALL_HINTS = {
    "git": "install git",
    "gh": "install GitHub CLI: https://cli.github.com",
    "claude": "install Claude Code (claude command)",
    "codex": "install Codex CLI (codex command)",
}
REMINDERS = (
    "Agents signed in on this machine, with access to the chosen models",
    "Git permissions and credentials to push the branch",
    "Skill dependencies installed and skills discovered by each agent",
    "Tools, dependencies and services the project needs",
)
FINAL_REMINDERS = (
    "Spec, acceptance criteria, children, labels and GitHub dependencies reviewed",
    "Autonomous agent access accepted: Ralph bypasses approvals and the sandbox",
)
LABEL_DESCRIPTIONS = {
    "needs_triage": "Maintainer needs to evaluate this issue",
    "needs_info": "Waiting on reporter for more information",
    "ready_for_agent": "Fully specified, ready for an AFK agent",
    "ready_for_human": "Requires human implementation",
    "wontfix": "Will not be actioned",
    "agent_working": "A Ralph agent is working on this child",
    "human_review": "Ralph stopped; a human must review",
    "implemented": "Implemented by Ralph",
    "work_started": "Ralph has started work on this parent",
}
ERRORS = (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError, ImportError, SyntaxError)
# Older gh prints an empty job log, with success, when the run's log archive has no step logs.
MINIMUM_GH_VERSION = (2, 70, 0)
GH_VERSION = re.compile(r"^gh version (\d+)\.(\d+)\.(\d+)\b")


def placeholder(value):
    if isinstance(value, str):
        return any(marker in value for marker in ("YOUR_", "CHOOSE_", "REPLACE_"))
    if isinstance(value, dict):
        return any(placeholder(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(placeholder(item) for item in value)
    return False


@dataclass
class Prerequisite:
    name: str
    detail: str
    problem: str | None = None
    fix: str | None = None


def prerequisites(which, read_version, engines=None):
    """Tools init and check both need.

    read_version(executable) -> its `--version` output, or None when it cannot be read.
    Without engines (init), any installed agent CLI is enough; otherwise each configured engine's CLI is required.
    """
    version = ".".join(map(str, sys.version_info[:3]))
    yield Prerequisite("Python", version, None if sys.version_info >= (3, 11) else f"Python {version}, 3.11 or newer required", "install Python 3.11 or newer")

    def tool(executable):
        path = which(executable)
        return Prerequisite(executable, path or "", None if path else f"{executable} not found in PATH", INSTALL_HINTS[executable])

    yield tool("git")
    yield gh_prerequisite(which, read_version)
    if engines is None:
        found = [executable for executable in AGENT_CLIS if which(executable)]
        names = " or ".join(f"{executable} ({name})" for executable, name in AGENT_CLIS.items())
        yield Prerequisite("Agent CLI", ", ".join(found), None if found else "no agent CLI found in PATH", f"install {names}")
    else:
        for executable in sorted(engines):
            yield tool(executable)


def gh_prerequisite(which, read_version):
    path = which("gh")
    if not path:
        return Prerequisite("gh", "", "gh not found in PATH", INSTALL_HINTS["gh"])
    match = GH_VERSION.match(read_version("gh") or "")
    if not match:
        return Prerequisite("gh", path, "gh --version is unreadable", INSTALL_HINTS["gh"])
    found = tuple(map(int, match.groups()))
    minimum = ".".join(map(str, MINIMUM_GH_VERSION))
    problem = None if found >= MINIMUM_GH_VERSION else f"gh {'.'.join(map(str, found))}, {minimum} or newer required"
    return Prerequisite("gh", f"{'.'.join(map(str, found))} ({path})", problem, f"update GitHub CLI to {minimum} or newer: https://cli.github.com")


OPTION = r"-[\w-]+(?:[ =]?[<\[][^>\]]*[>\]](?:\.\.\.)?)?"
OPTION_LINE = re.compile(rf"( *)({OPTION}(?:, {OPTION})*)(?=\s{{2,}}|$)")
FLAG = re.compile(r"(?<![\w\[-])-{1,2}\w[\w-]*")
SHORT_FLAG_COLUMN = len("-c, ")


def listed_flags(help_text):
    """Flags a CLI help page defines.

    Option lines sit at the shallowest option indent, or one short-flag column deeper for long-only options;
    flags mentioned in descriptions, including wrapped description lines, are not listed.
    """
    options = [(len(match[1]), match[2]) for line in help_text.splitlines() if (match := OPTION_LINE.match(line))]
    if not options:
        return set()
    indent = min(depth for depth, _ in options)
    return {flag for depth, names in options if depth <= indent + SHORT_FLAG_COLUMN for flag in FLAG.findall(names)}


def check_engine_flags(command, engine):
    """Confirm the installed engine's help lists every flag Ralph passes to it; no network or tokens."""
    help_command, flags = engine_help_command_and_flags(engine, PACKAGE_DIRECTORY / "outcome-schema.json")
    listed = listed_flags(command(*help_command))
    missing = [flag for flag in flags if flag not in listed]
    name = AGENT_CLIS[engine]
    require(not missing, f"{name} does not list {', '.join(missing)} in {' '.join(help_command)}", f"update {name}, or check that ralph-kit supports the installed version")
    return f"{len(flags)} flags listed"


def missing_labels(command, repository, wanted):
    pages = json.loads(command("gh", "api", "--paginate", "--slurp", f"repos/{repository}/labels"))
    present = {label["name"] for page in pages for label in page}
    return sorted(set(wanted) - present)


def create_labels(command, repository, labels):
    """Create each {role: name} label; return the names gh could not create.

    command either returns None or raises RuntimeError on failure.
    """
    failed = []
    for role, name in labels.items():
        try:
            created = command("gh", "label", "create", name, "--repo", repository, "--description", LABEL_DESCRIPTIONS.get(role, ""))
        except RuntimeError:
            created = None
        if created is None:
            failed.append(name)
    return failed


def arrow_select(question, options, default):
    """Pick an option with the arrow keys; needs a real terminal on stdin and stdout. Ctrl-C raises KeyboardInterrupt.

    An option (value, label, reason) is greyed out with its reason and cannot be selected.
    """
    import questionary

    choices = [
        questionary.Choice(f"{value}  ({label})" if label else value, value=value, disabled=reason[0] if reason else None)
        for value, label, *reason in options
    ]
    return questionary.select(question, choices, default=default, instruction="(↑↓ to move, Enter to select)").unsafe_ask()


def confirm_with_arrows(question):
    """Yes or Later; Ctrl-C counts as Later."""
    try:
        return arrow_select(question, [("Yes", ""), ("Later", "")], "Yes") == "Yes"
    except KeyboardInterrupt:
        return False


class Blocked(ValueError):
    """A failed check, with the action that fixes it."""

    def __init__(self, message, fix=None):
        super().__init__(message)
        self.fix = fix


def require(condition, message, fix=None):
    if not condition:
        raise Blocked(message, fix)


@dataclass
class Result:
    status: str  # ok, warn (never blocks), fail, skip (not verified) or off (disabled)
    label: str
    detail: str = ""
    fix: str | None = None


class Readiness:
    def __init__(self, root):
        self.root = root
        self.sections: list[tuple[str, list[Result]]] = []

    @property
    def errors(self):
        return sum(result.status == "fail" for _, results in self.sections for result in results)

    def section(self, title):
        self.sections.append((title, []))

    def add(self, status, label, detail="", fix=None):
        self.sections[-1][1].append(Result(status, label, detail, fix))
        return status == "ok"

    def check(self, label, action, fix=None):
        """Run action, which returns the detail shown on success; True when it passed."""
        try:
            detail = action()
        except ERRORS as error:
            return self.add("fail", label, str(error), getattr(error, "fix", None) or fix)
        return self.add("ok", label, detail or "")

    def skip(self, label, reason):
        self.add("skip", label, f"not verified: {reason}")

    def command(self, *args):
        try:
            result = subprocess.run(args, cwd=self.root, capture_output=True, text=True, timeout=30, env=os.environ | {"GIT_OPTIONAL_LOCKS": "0"})
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{args[0]}: timed out after 30 seconds") from None
        if result.returncode:
            # CLI output can contain credentials; do not echo it.
            raise RuntimeError(f"{args[0]} {' '.join(args[1:])} failed; check access and authentication")
        return result.stdout.strip()


ICONS = {"ok": ("✓", "green"), "warn": ("!", "yellow"), "fail": ("✗", "bold red"), "skip": ("○", "yellow"), "off": ("–", "dim")}


def render(console, report, repository=None, reminders=()):
    title = Text("ralph-kit check", style="bold")
    if repository:
        title.append(f" · {repository}", style="dim")
    console.print(title)
    # One label width for every section keeps the details aligned down the page.
    label_width = max(len(result.label) for _, results in report.sections for result in results)
    for name, results in report.sections:
        console.print()
        console.print(name, style="bold cyan")
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=1)
        grid.add_column(width=label_width + 2, no_wrap=True)
        grid.add_column(overflow="fold")
        for result in results:
            icon, style = ICONS[result.status]
            detail = Text(result.detail, style="red" if result.status == "fail" else "dim")
            if result.fix:
                detail.append(f"\n→ {result.fix}", style="yellow")
            grid.add_row(Text(icon, style=style), Text(result.label, style="red" if result.status == "fail" else ""), detail)
        console.print(Padding(grid, (0, 0, 0, 2)))

    counts = {status: sum(result.status == status for _, results in report.sections for result in results) for status in ICONS}
    console.print()
    summary = Text()
    if counts["fail"]:
        summary.append(f"✗ {counts['fail']} {'blocker' if counts['fail'] == 1 else 'blockers'}", style="bold red")
    else:
        summary.append("✓ No blockers", style="bold green")
    summary.append(f" · {counts['ok']} OK", style="dim")
    if counts["warn"]:
        summary.append(f" · {counts['warn']} {'warning' if counts['warn'] == 1 else 'warnings'}", style="yellow")
    if counts["skip"]:
        summary.append(f" · {counts['skip']} not verified", style="dim")
    console.print(summary)
    if counts["fail"]:
        console.print("Fix the items in red, then run ralph-kit check again.", style="dim")
    if reminders:
        console.print()
        console.print("Not checked by ralph-kit; verify before launching:", style="bold")
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=1)
        grid.add_column()
        for reminder in reminders:
            grid.add_row("·", reminder, style="dim")
        console.print(Padding(grid, (0, 0, 0, 2)))


def check_policy(config, root):
    spec = importlib.util.spec_from_file_location("ralph_readiness_quality", config.quality_module)
    require(spec is not None and spec.loader is not None, "quality module not found")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("load_manifest", "matching_local_gates", "ci_requirements", "validate_check_evidence", "changed_paths"):
        require(callable(getattr(module, name, None)), f"missing function {name}")
    manifest = module.load_manifest(config.quality_manifest)
    found = []
    if config.local_checks:
        gates = [gate for gate in manifest["local_gates"] if gate.get("before_agent_review")]
        require(gates, "no local gate usable by Ralph", "add a check with before_agent_review to the manifest")
        for gate in gates:
            require(not placeholder(gate), "local check still has placeholders", "fill in the command in the manifest")
            command = gate.get("command")
            require(isinstance(command, list) and command and all(isinstance(arg, str) and arg for arg in command), "invalid local command")
            executable = command[0]
            available = shutil.which(executable) if "/" not in executable else shutil.which(str(root / executable))
            require(available, f"local command not found: {executable}", f"install {executable} or fix the command")
        found.append(f"{len(gates)} local check{'s' if len(gates) > 1 else ''}")
    if config.ci_checks:
        checks = manifest["ci"]["checks"]
        require(checks, "no CI check configured")
        names = [check["name"] for check in checks.values()]
        require(all(isinstance(name, str) and name.strip() for name in names), "invalid CI check name")
        require(not placeholder(checks), "CI checks still have placeholders", "fill in the check names in the manifest")
        require(len(set(names)) == len(names), "duplicate CI check names")
        module.ci_requirements(manifest, [], final=True)
        found.append(f"{len(names)} check{'s' if len(names) > 1 else ''} CI")
    return ", ".join(found) + " (not run)"


def run(root, config_path, *, with_review=False, console=None, confirm=None, command=None):
    """confirm(question) -> bool offers to create missing labels; None never writes to GitHub.

    command(*args) -> stdout runs git, gh and engine help commands; it raises on failure.
    A terminal sees a spinner until the report is ready.
    """
    console = console or Console(highlight=False)
    with contextlib.ExitStack() as checking:
        checking.enter_context(spinner("Checking that this repository is ready for Ralph…", console))
        return readiness(root, config_path, with_review=with_review, console=console, confirm=confirm, command=command, checked=checking.close)


def readiness(root, config_path, *, with_review, console, confirm, command, checked):
    """Run every check, call checked() once they are done, then render the report."""
    report = Readiness(root)
    command = command or report.command
    report.section("Configuration")
    try:
        config = load_config(config_path, root, require_review=with_review)
        # The setting lives in the configuration whose models.review it makes required.
        if config.review_by_default and not with_review:
            with_review = True
            config = load_config(config_path, root, require_review=True)
        roles = ("implementation", "correction", "review") if with_review else ("implementation", "correction")
        profiles = [config.models[role] for role in roles] + ([config.backup] if config.backup else [])
        require(not placeholder([config.repository, config.trusted_authors, [profile.name for profile in profiles]]), "repository, authors or models still have placeholders")
    except MissingConfigError:
        report.add("fail", config_path.name, "not found: this repository is not set up yet", "ralph-kit init")
        checked()
        render(console, report)
        return 1
    except (RalphConfigError, ValueError) as error:
        report.add("fail", config_path.name, str(error), f"edit {config_path.name} by hand (ralph-kit init --force would start over and lose its settings)")
        checked()
        render(console, report)
        return 1
    report.add("ok", config_path.name, "values filled in")
    if config.defaulted_labels:
        roles = sorted(config.defaulted_labels)
        names = ", ".join(config.labels[role] for role in roles)
        report.add(
            "warn",
            "Label roles",
            f"{config_path.name} leaves out {', '.join(roles)}, so {names} {'is' if len(roles) == 1 else 'are'} used",
            "ralph-kit update-labels writes it" if len(roles) == 1 else "ralph-kit update-labels writes them",
        )

    report.section("Tools")
    engines = {profile.engine for profile in profiles}
    available = set()
    def read_version(executable):
        try:
            return command(executable, "--version")
        except ERRORS:
            return None

    for item in prerequisites(shutil.which, read_version, engines):
        if report.add("fail" if item.problem else "ok", item.name, item.problem or item.detail, item.fix if item.problem else None):
            available.add(item.name)

    def package_files():
        schema = json.loads((PACKAGE_DIRECTORY / "outcome-schema.json").read_text())
        require(isinstance(schema, dict) and schema.get("type") == "object", "invalid result schema")
        return f"{__version__}, files complete"
    report.check("ralph-kit", package_files, "reinstall ralph-kit")
    if config.caffeinate_by_default:
        # A run continues without a sleep lock, so its absence only warns.
        helper, problem = keep_awake.availability(sys.platform)
        if problem:
            fix = f"install {helper}, or set caffeinate_by_default = false" if helper else "set caffeinate_by_default = false"
            report.add("warn", "Sleep lock", f"{problem}; the machine may sleep during runs", fix)
        else:
            report.add("ok", "Sleep lock", helper)

    report.section("Git")
    if "git" not in available:
        for label in ("Repository root", "Remote", "Branch", "Working tree", "Run diagnostics"):
            report.skip(label, "git missing")
    else:
        def repository_root():
            toplevel = pathlib.Path(command("git", "rev-parse", "--show-toplevel")).resolve()
            require(toplevel == root, f"run from a subdirectory of {toplevel}", f"cd {shlex.quote(str(toplevel))}")
            return str(root)
        report.check("Repository root", repository_root)

        def remote():
            url = command("git", "remote", "get-url", config.remote)
            return f"{config.remote} → {re.sub(r'//[^/@]+@', '//', url)}"  # Hide credentials embedded in HTTPS URLs.
        report.check("Remote", remote, f"git remote add {config.remote} <url>, or fix remote in ralph.toml")

        def branch():
            command("git", "check-ref-format", "--branch", config.base_branch)
            try:
                current = command("git", "symbolic-ref", "--quiet", "--short", "HEAD")
            except RuntimeError:
                raise Blocked("detached HEAD, no active branch", "git switch -c <branch-name>") from None
            if current == config.base_branch:
                return f"{current} (run will offer a ralph-kit/issue-<parent> branch)"
            return f"{current} (base: {config.base_branch})"
        report.check("Branch", branch)

        def worktree():
            changes = command("git", "status", "--porcelain").splitlines()
            count = len(changes)
            require(not changes, f"{count} modified or untracked file{'s' if count > 1 else ''}", "commit the changes, or stash them with git stash")
            return "clean"
        report.check("Working tree", worktree)

        def diagnostics_ignored():
            entry = f"{DIAGNOSTICS_DIRECTORY}/"
            try:
                command("git", "check-ignore", "--quiet", entry)
            except RuntimeError:  # check-ignore fails when no rule matches; Repository root already proved Git works.
                raise Blocked(f"{entry} is not ignored by Git", f'echo "{entry}" >> .gitignore, then commit .gitignore') from None
            return f"{entry} ignored by Git"
        report.check("Run diagnostics", diagnostics_ignored)

    report.section("GitHub")
    missing = []
    if "gh" not in available:
        for label in ("Repository", "gh account", "Trusted account", "Write access", "Labels"):
            report.skip(label, "gh missing")
    else:
        def repository():
            # Default repo must match, because a run uses gh repo view the same way.
            detected = json.loads(command("gh", "repo", "view", "--json", "nameWithOwner"))["nameWithOwner"]
            require(detected == config.repository, f"gh detects {detected}, ralph.toml says {config.repository}", f"fix repository in ralph.toml, or gh repo set-default {config.repository}")
            return config.repository
        report.check("Repository", repository)

        login = None

        def account():
            nonlocal login
            login = json.loads(command("gh", "api", "user"))["login"]
            return login
        if report.check("gh account", account, "gh auth login"):
            report.check("Trusted account", lambda: require(login in config.trusted_authors, f"{login} is not in trusted_authors", f"add {login} to trusted_authors in ralph.toml") or "in trusted_authors")

            def write_access():
                details = json.loads(command("gh", "api", f"repos/{config.repository}"))
                require(details.get("permissions", {}).get("push"), f"{login} has no write access", f"ask for write access to {config.repository}")
                return "push allowed"
            report.check("Write access", write_access)

            def labels():
                wanted = set(config.labels.values())
                missing[:] = missing_labels(command, config.repository, wanted)
                names = " ".join(shlex.quote(name) for name in missing)
                fix = f"gh label create {names}" if len(missing) == 1 else f'for label in {names}; do gh label create "$label"; done'
                require(not missing, f"{len(missing)} missing: {', '.join(missing)}", fix)
                return f"all {len(wanted)} present"
            report.check("Labels", labels)
        else:
            for label in ("Trusted account", "Write access", "Labels"):
                report.skip(label, "gh account unavailable")

    report.section("Agents")
    for engine in sorted(engines):
        directory = ".claude/skills" if engine == "claude" else ".agents/skills"
        path = f"{directory}/implement/SKILL.md"
        report.check(f"Skill implement ({engine})", lambda path=path, directory=directory: require((root / path).is_file(), f"{path} missing or broken link", f"add the implement skill to {directory}/") or path)
        if engine in available:
            report.check(f"Flags ({engine})", lambda engine=engine: check_engine_flags(command, engine), f"reinstall {AGENT_CLIS[engine]}")
        else:
            report.skip(f"Flags ({engine})", f"{engine} missing")

    report.section("Quality checks")
    if config.verification.needs_quality_policy:
        report.check("Module and manifest", lambda: check_policy(config.verification, root))
    else:
        report.add("off", "Checks", "local and CI checks disabled in ralph.toml")

    reminders = list(REMINDERS)
    if config.verification.local_checks:
        reminders.append("Local check commands run and their results verified")
    if config.verification.ci_checks:
        reminders.append("CI workflow check names confirmed and workflow working")
    checked()
    render(console, report, config.repository, [*reminders, *FINAL_REMINDERS])
    errors = report.errors
    if missing and confirm:
        console.print()
        count = f"{len(missing)} missing label{'s' if len(missing) > 1 else ''}"
        if confirm(f"Create the {count} on {config.repository}?"):
            roles = {name: role for role, name in config.labels.items()}
            with spinner(f"Creating the {count} on {config.repository}…", console):
                failed = create_labels(command, config.repository, {roles[name]: name for name in missing})
            for name in missing:
                console.print(Text(f"✗ {name} not created", style="red") if name in failed else Text(f"✓ {name} created", style="green"))
            errors -= not failed
        else:
            console.print("Labels not created; ralph-kit check will offer again.", style="dim")
    return int(bool(errors))


def main(argv=None, prog=None):
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path("ralph.toml"))
    parser.add_argument("--review", "--with-review", dest="with_review", action="store_true", help="also check the independent review role (implied by review_by_default = true)")
    args = parser.parse_args(argv)
    root = pathlib.Path.cwd().resolve()
    path = args.config if args.config.is_absolute() else root / args.config
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    return run(root, path, with_review=args.with_review, confirm=confirm_with_arrows if interactive else None)
