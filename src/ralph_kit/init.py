"""`ralph-kit init`: detect what can be read locally, ask for the rest, write the configuration."""

from __future__ import annotations

import contextlib
import json
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import Any, TextIO

from .check import AGENT_CLIS as ENGINES, create_labels, missing_labels, prerequisites
from .config import (
    BACKUP_MODEL_KEY,
    CLAUDE_REASONING_EFFORTS,
    DEFAULT_CHILD_SESSION_LIMITS,
    DEFAULT_LABELS,
    DIAGNOSTICS_DIRECTORY,
    MODEL_ROLES,
    AgentProfile,
    ChildSessionLimits,
    RalphConfig,
    RalphConfigError,
    VerificationConfig,
    load_config,
)


CONFIG_NAME = "ralph.toml"
GITIGNORE = ".gitignore"
QUALITY_MODULE = ".github/scripts/quality.py"
QUALITY_MANIFEST = ".github/quality-gates.json"
LABELS = DEFAULT_LABELS
# Claude Code has no command listing models; its documented aliases follow the latest version.
CLAUDE_MODELS = [
    ("fable", "Fable, for the hardest and longest tasks"),
    ("opus", "latest Opus, complex reasoning"),
    ("sonnet", "latest Sonnet, everyday coding"),
    ("haiku", "latest Haiku, fast and cheap"),
]
DEFAULT_MODELS = {"claude": {"implementation": "sonnet", "review": "opus", "correction": "sonnet", "backup": "opus"}}
EFFORTS = {
    "claude": sorted(CLAUDE_REASONING_EFFORTS, key=["low", "medium", "high", "xhigh", "max"].index),
    "codex": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
}
OTHER = "other"
NO_BACKUP = "No backup"
PLACEHOLDERS = {
    "repository": "YOUR_ORG/YOUR_REPO",
    "author": "YOUR_GITHUB_ACCOUNT",
    "implementation": "CHOOSE_IMPLEMENTATION_MODEL",
    "review": "CHOOSE_REVIEW_MODEL",
    "correction": "CHOOSE_CORRECTION_MODEL",
    "backup": "CHOOSE_MODEL_FOR_BACKUP",
}

CommandRunner = Callable[..., "str | None"]
# (value, label) or (value, label, reason): a reason marks the option shown but unavailable.
Option = tuple[str, str] | tuple[str, str, str | None]
Selector = Callable[[str, list[Option], str], str]


def option(value: str, label: str, reason: str | None = None) -> Option:
    """An option, shown but unavailable when there is a reason."""
    return (value, label, reason) if reason else (value, label)


def unavailable(choice: Option) -> str | None:
    """Why the option cannot be selected, or None when it can."""
    return choice[2] if len(choice) > 2 else None


class InitError(RuntimeError):
    """init cannot write a configuration in this directory."""


class InitAborted(RuntimeError):
    """The user interrupted the questions; nothing was written."""


def run_command(*args: str, cwd: pathlib.Path) -> str | None:
    """stdout on success, None when the tool is missing or fails."""
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


class Prompter:
    """Ask questions on a terminal; without one, keep defaults and placeholders."""

    def __init__(
        self,
        interactive: bool,
        read: Callable[[str], str] = input,
        output: TextIO = sys.stdout,
        select: Selector | None = None,
        spinner: Callable[[str], contextlib.AbstractContextManager[Any]] | None = None,
    ):
        self.interactive = interactive
        self.read = read
        self.output = output
        self.select = select
        self.spinner = spinner

    def say(self, message: str) -> None:
        print(message, file=self.output)

    def working(self, message: str) -> contextlib.AbstractContextManager[Any]:
        """A spinner showing `message` while gh or git answers, when one was given; never around a question."""
        return self.spinner(message) if self.spinner else contextlib.nullcontext()

    def _read(self, prompt: str) -> str:
        try:
            return self.read(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise InitAborted("init cancelled, nothing written") from None

    def choice(self, question: str, options: list[Option], default: str) -> str:
        reasons = {choice[0]: reason for choice in options if (reason := unavailable(choice))}
        if default in reasons:
            default = next(choice[0] for choice in options if choice[0] not in reasons)
        if not self.interactive:
            return default
        if self.select:
            try:
                return self.select(question, options, default)
            except KeyboardInterrupt:
                raise InitAborted("init cancelled, nothing written") from None
        # Numbered fallback when no arrow-key selector is available.
        values = [choice[0] for choice in options]
        self.say(f"\n? {question}")
        for index, choice in enumerate(options, start=1):
            value, label = choice[:2]
            reason = f"  (unavailable: {reasons[value]})" if value in reasons else ""
            self.say(f"  {index}) {value}{f'  ({label})' if label else ''}{reason}")
        default_index = values.index(default) + 1
        while True:
            answer = self._read(f"Choice [{default_index}]: ")
            if not answer:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(values):
                answer = values[int(answer) - 1]
            if answer in reasons:
                self.say(f"  {answer} is unavailable: {reasons[answer]}.")
            elif answer in values:
                return answer
            else:
                self.say(f"  Expected a number between 1 and {len(values)}.")

    def text(
        self,
        question: str,
        *,
        default: str | None = None,
        placeholder: str | None = None,
        validate: Callable[[str], str | None] | None = None,
        required: bool = True,
    ) -> str:
        if not self.interactive:
            return placeholder if placeholder is not None else default or ""
        self.say(f"\n? {question}")
        while True:
            answer = self._read(f"Answer [{default}]: " if default else "Answer: ") or (default or "")
            if not answer and not required:
                return answer
            error = "an answer is required" if not answer else (validate(answer) if validate else None)
            if error is None:
                return answer
            self.say(f"  {error}")


def repository_error(value: str) -> str | None:
    return None if re.fullmatch(r"[^/\s]+/[^/\s]+", value) else "expected format: owner/name"


def git_name_error(value: str) -> str | None:
    if value.startswith("-") or any(character.isspace() for character in value):
        return "invalid Git name (no spaces, must not start with -)"
    return None


def _authors(value: str) -> list[str]:
    return list(dict.fromkeys(author.strip() for author in value.split(",") if author.strip()))


def current_login(command: CommandRunner) -> str | None:
    """The current gh account, or None without gh."""
    return command("gh", "api", "user", "--jq", ".login")


def detect_repository(command: CommandRunner) -> tuple[str | None, str | None]:
    """The gh repository of the working directory and its default branch; None for what gh cannot tell."""
    output = command("gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef")
    if not output:
        return None, None
    try:
        details = json.loads(output)
    except json.JSONDecodeError:
        return None, None
    branch = (details.get("defaultBranchRef") or {}).get("name") or None
    return details.get("nameWithOwner") or None, branch


def remote_default_branch(command: CommandRunner, remote: str) -> str | None:
    """The branch the remote's HEAD points to, as last fetched; None when Git does not know it."""
    head = command("git", "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
    return head.split("/", 1)[1] if head and "/" in head else None


def git_remotes(command: CommandRunner) -> list[str]:
    """The repository's Git remotes; [] without git output."""
    return (command("git", "remote") or "").split()


def codex_catalog(command: CommandRunner) -> list[dict]:
    """Models listed by Codex for this account, best first; [] when the debug command is unavailable."""
    output = command("codex", "debug", "models")
    try:
        models = json.loads(output)["models"] if output else []
        listed = [
            {
                "name": model["slug"],
                "description": model.get("description") or "",
                "efforts": [level["effort"] for level in model.get("supported_reasoning_levels") or []],
                "priority": model.get("priority", 0),
            }
            for model in models
            if model.get("visibility") == "list"
        ]
    except (TypeError, KeyError, ValueError):
        return []
    return sorted(listed, key=lambda model: model["priority"])


def _model_efforts(engine: str, catalog: list[dict], name: str) -> list[str]:
    model = next((model for model in catalog if model["name"] == name), None)
    supported = [effort for effort in model["efforts"] if effort in EFFORTS[engine]] if model else []
    return supported or EFFORTS[engine]


def _effort_clashes(engine: str, catalog: list[dict], name: str, avoid: dict[str, dict]) -> dict[str, list[str]]:
    """Each reasoning effort of the model, with the avoided session roles whose model profile it would duplicate."""
    return {
        effort: [
            role for role, profile in avoid.items()
            if (profile["engine"], profile["name"], profile["reasoning_effort"]) == (engine, name, effort)
        ]
        for effort in _model_efforts(engine, catalog, name)
    }


def _model_unavailable(engine: str, catalog: list[dict], name: str, avoid: dict[str, dict]) -> str | None:
    """Why no reasoning effort of the model is left, or None while one is."""
    clashes = _effort_clashes(engine, catalog, name, avoid)
    if not all(clashes.values()):
        return None
    roles = dict.fromkeys(role for roles in clashes.values() for role in roles)
    return f"every reasoning effort same as {' or '.join(roles)}"


def _choose_model(
    prompter: Prompter,
    engine: str,
    catalog: list[dict],
    role: str,
    question: str,
    avoid: dict[str, dict],
    current: str | None = None,
) -> str:
    if engine == "claude":
        options = CLAUDE_MODELS
        default = DEFAULT_MODELS["claude"][role]
    else:
        options = [(model["name"], model["description"]) for model in catalog]
        default = options[0][0] if options else None
    if current:  # Enter keeps the current model, listed or not.
        if options and current not in (name for name, _ in options):
            options = [(current, "current model"), *options]
        default = current
    if not options:
        def ask() -> str:
            return prompter.text(
                f"{question} (Codex model list unavailable)", default=current, placeholder=PLACEHOLDERS[role]
            )
    elif not prompter.interactive:
        return PLACEHOLDERS[role]
    else:
        choices = [option(name, label, _model_unavailable(engine, catalog, name, avoid)) for name, label in options]
        choices.append((OTHER, "enter a full model name"))

        def ask() -> str:
            name = prompter.choice(question, choices, default)
            return prompter.text("Model name", placeholder=PLACEHOLDERS[role]) if name == OTHER else name
    # A free model name cannot be marked unavailable in advance.
    name = ask()
    while reason := _model_unavailable(engine, catalog, name, avoid):
        prompter.say(f"  {name} is unavailable: {reason}.")
        name = ask()
    return name


def ask_profile(
    prompter: Prompter,
    engine: str,
    catalog: list[dict],
    role: str,
    question: str,
    effort: str,
    avoid: dict[str, dict] | None = None,
    current: str | None = None,
) -> dict:
    """A model profile; choices that would duplicate a model profile in avoid are shown unavailable.

    current is the model to offer by default, when the role already has one on this engine.
    """
    avoid = avoid or {}
    name = _choose_model(prompter, engine, catalog, role, question, avoid, current)
    clashes = _effort_clashes(engine, catalog, name, avoid)
    efforts = list(clashes)
    preferred = [effort, efforts[len(efforts) // 2], *efforts]
    return {
        "engine": engine,
        "name": name,
        "display_name": name,
        "reasoning_effort": prompter.choice(
            f"Reasoning effort for {name}",
            [option(level, "", f"same as {' and '.join(roles)}" if roles else None) for level, roles in clashes.items()],
            next(level for level in preferred if level in clashes and not clashes[level]),
        ),
    }


def backup_engines(used: set[str], installed: list[str], current: str | None = None) -> list[Option]:
    """The backup's engines: an installed CLI no session role uses comes first, since an engine failure rarely spans CLIs.

    current, the backup's engine, stays offered even when its CLI is not installed here; with no CLI at all, every engine is.
    """
    engines = [engine for engine in ENGINES if engine in installed or engine == current] if installed else list(ENGINES)
    others = [engine for engine in engines if engine in installed and engine not in used]
    options = [(engine, f"{ENGINES[engine]}, recommended: a different CLI") for engine in others]
    return options + [
        (engine, ENGINES[engine] + ("" if engine in installed else ", not installed here"))
        for engine in engines
        if engine not in others
    ]


def _engine_for_backup(prompter: Prompter, engine: str, installed: list[str]) -> str:
    """Engine of the backup or NO_BACKUP, preselecting another installed CLI when there is one."""
    options = backup_engines({engine}, installed)
    return prompter.choice(
        "Backup, taking over every session after an engine failure",
        [*options, (NO_BACKUP, "the run stops at an engine failure")],
        options[0][0] if options[0][0] != engine else NO_BACKUP,
    )


def collect(root: pathlib.Path, prompter: Prompter, command: CommandRunner, which: Callable[[str], str | None]) -> dict:
    say = prompter.say
    say("🔎 Detecting from git and gh…")

    with prompter.working("Detecting the GitHub repository…"):
        repository, base_branch = detect_repository(command)
    if repository:
        say(f"  ✓ GitHub repository: {repository}")
    else:
        repository = prompter.text(
            "GitHub repository (owner/name); gh could not detect it",
            placeholder=PLACEHOLDERS["repository"],
            validate=repository_error,
        )

    if not base_branch:
        base_branch = remote_default_branch(command, "origin")
    if base_branch:
        say(f"  ✓ Base branch: {base_branch}")
    else:
        base_branch = prompter.text("Base branch (Ralph never commits to it)", default="main", validate=git_name_error)

    remotes = git_remotes(command)
    if len(remotes) == 1:
        remote = remotes[0]
        say(f"  ✓ Remote: {remote}")
    elif remotes:
        remote = prompter.choice(
            "Remote Ralph pushes to",
            [(name, command("git", "remote", "get-url", name) or "") for name in remotes],
            "origin" if "origin" in remotes else remotes[0],
        )
    else:
        remote = prompter.text("Remote Ralph pushes to (no Git remote found)", default="origin", validate=git_name_error)

    with prompter.working("Reading the current gh account…"):
        login = current_login(command)
    if login:
        authors = [login]
        say(f"  ✓ Trusted author: {login} (current gh account)")
    else:
        authors = _authors(prompter.text(
            "Trusted GitHub accounts, comma-separated (include the one Ralph runs as)",
            placeholder=PLACEHOLDERS["author"],
            validate=lambda value: None if _authors(value) else "at least one account",
        ))

    checks = (root / QUALITY_MODULE).is_file() and (root / QUALITY_MANIFEST).is_file()
    if checks:
        say(f"  ✓ Quality module found: local and CI checks enabled ({QUALITY_MODULE})")
    else:
        say(f"  ℹ No {QUALITY_MODULE} + {QUALITY_MANIFEST}: local and CI checks disabled")

    installed = [engine for engine in ENGINES if which(engine)]
    if len(installed) == 1:
        engine = installed[0]
        say(f"  ✓ Agent engine: {ENGINES[engine]} (only CLI installed)")
    else:
        engine = prompter.choice("Agent engine", [(name, ENGINES[name]) for name in installed], installed[0])
    catalogs: dict[str, list[dict]] = {}

    def catalog(name: str) -> list[dict]:
        if name not in catalogs:
            catalogs[name] = codex_catalog(command) if name == "codex" else []
        return catalogs[name]

    models = {}
    for role, question, effort in (
        ("implementation", "Model for implementation and corrections", "medium"),
        ("review", "Model for the independent review (reviewed runs)", "high"),
    ):
        models[role] = ask_profile(prompter, engine, catalog(engine), role, question, effort)
    models["correction"] = dict(models["implementation"])

    if (chosen := _engine_for_backup(prompter, engine, installed)) != NO_BACKUP:
        # Correction copies implementation, so avoiding implementation avoids both.
        avoid = {role: models[role] for role in ("implementation", "review")}
        models["backup"] = ask_profile(prompter, chosen, catalog(chosen), "backup", "Model for the backup", "medium", avoid)

    return {
        "create_labels": offer_labels(prompter, command, repository),
        "repository": repository,
        "base_branch": base_branch,
        "remote": remote,
        "trusted_authors": authors,
        "models": models,
        "local_checks": checks,
        "ci_checks": checks,
    }


def offer_labels(
    prompter: Prompter, command: CommandRunner, repository: str, labels: dict[str, str] = LABELS
) -> dict[str, str]:
    """Missing labels ({role: name}) the user agreed to create once the configuration is written."""
    if repository == PLACEHOLDERS["repository"]:
        return {}
    try:
        with prompter.working(f"Reading the labels of {repository}…"):
            missing = missing_labels(command, repository, labels.values())
    except (TypeError, KeyError, ValueError):  # gh unavailable or unexpected output: ralph-kit check reports it.
        return {}
    if not missing:
        prompter.say(f"  ✓ Labels: all {len(labels)} present")
        return {}
    prompter.say(f"  ℹ Labels missing on {repository}: {', '.join(missing)}")
    if not prompter.interactive:
        return {}
    answer = prompter.choice(
        f"Create the {len(missing)} missing label{'s' if len(missing) > 1 else ''}?",
        [("Yes", "create them now with gh"), ("Later", "ralph-kit check will offer again")],
        "Yes",
    )
    # In the file's order: a loaded configuration's labels come in no particular order.
    return {role: labels[role] for role in LABELS if labels[role] in missing} if answer == "Yes" else {}


def create_offered_labels(prompter: Prompter, command: CommandRunner, repository: str, labels: dict[str, str]) -> None:
    """Create the labels offer_labels returned, once the configuration is written, reporting each one."""
    with prompter.working(f"Creating the labels on {repository}…"):
        failed = create_labels(command, repository, labels)
    for name in labels.values():
        prompter.say(f"   ✗ Label {name} not created" if name in failed else f"   ✓ Label {name} created")


def _string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


# The comment init writes above each top-level setting a configuration may leave out, in the file's order.
TOP_LEVEL_COMMENTS = {
    "review_by_default": "# true makes every run a reviewed run; ralph-kit run --no-review overrides it.",
    "caffeinate_by_default": "# true makes every run hold a sleep lock; ralph-kit run --no-caffeinate overrides it.",
}


def top_level_setting(key: str, value: bool) -> list[str]:
    """The lines init writes for a top-level setting a configuration may leave out: its comment, then key = value."""
    return [TOP_LEVEL_COMMENTS[key], f"{key} = {str(value).lower()}"]


CHILD_SESSION_LIMIT_HEADER = "[child_session_limit]"


def child_session_limit_table(limits: ChildSessionLimits) -> list[str]:
    """The lines init writes for the child session limits: the table header, its comment, then each limit."""
    return [
        CHILD_SESSION_LIMIT_HEADER,
        "# The most agent sessions one child may receive in a run. The correction limit (three corrections)",
        "# bounds a child at 4 sessions in a standard run and 8 in a reviewed run, so higher values have no effect.",
        *(f"{key} = {getattr(limits, key)}" for key in DEFAULT_CHILD_SESSION_LIMITS),
    ]


def configuration(values: dict, root: pathlib.Path) -> RalphConfig:
    """The configuration init collected, in the shape load_config returns."""
    models = {role: AgentProfile(**profile) for role, profile in values["models"].items()}
    return RalphConfig(
        repository=values["repository"],
        base_branch=values["base_branch"],
        remote=values["remote"],
        trusted_authors=frozenset(values["trusted_authors"]),
        labels=dict(LABELS),
        models={role: profile for role, profile in models.items() if role != BACKUP_MODEL_KEY},
        backup=models.get(BACKUP_MODEL_KEY),
        verification=VerificationConfig(
            local_checks=values["local_checks"],
            ci_checks=values["ci_checks"],
            quality_module=root / QUALITY_MODULE,
            quality_manifest=root / QUALITY_MANIFEST,
        ),
    )


def render(config: RalphConfig, root: pathlib.Path) -> str:
    """The TOML file for a configuration; loading it with load_config gives the same configuration back."""
    lines = [
        "# Ralph configuration for this repository. See https://github.com/lmpaul/ralph-kit",
        "version = 1",
        f"repository = {_string(config.repository)}",
        f"base_branch = {_string(config.base_branch)}",
        f"remote = {_string(config.remote)}",
        # A set has no order of its own; sorting keeps the file stable from one save to the next.
        f"trusted_authors = [{', '.join(_string(author) for author in sorted(config.trusted_authors))}]",
        *(line for key in TOP_LEVEL_COMMENTS for line in top_level_setting(key, getattr(config, key))),
        "",
        "[labels]",
        *(f"{key} = {_string(config.labels[key])}" for key in LABELS),
    ]
    profiles = {**config.models, BACKUP_MODEL_KEY: config.backup}
    for role in (*MODEL_ROLES, BACKUP_MODEL_KEY):
        profile = profiles.get(role)
        if profile is None:
            continue
        lines.append("")
        if role == "review":
            lines.append("# Used by reviewed runs: ralph-kit run --review, or every run when review_by_default = true.")
        if role == BACKUP_MODEL_KEY:
            lines.append("# Takes over every session for the rest of a run after an engine failure.")
        lines.append(f"[models.{role}]")
        lines += [
            f"{key} = {_string(getattr(profile, key))}" for key in ("engine", "name", "display_name", "reasoning_effort")
        ]
    verification = config.verification
    lines += [
        "",
        "[verification]",
        "# Both checks need the quality module and manifest below; disabled checks do not load them.",
        f"local_checks = {str(verification.local_checks).lower()}",
        f"ci_checks = {str(verification.ci_checks).lower()}",
        f"quality_module = {_string(verification.quality_module.relative_to(root).as_posix())}",
        f"quality_manifest = {_string(verification.quality_manifest.relative_to(root).as_posix())}",
        "",
        *child_session_limit_table(config.child_session_limits),
    ]
    return "\n".join(lines) + "\n"


def ignore_diagnostics(root: pathlib.Path, command: CommandRunner) -> bool:
    """Append the run diagnostics directory to .gitignore unless Git already ignores it; True when .gitignore changed."""
    entry = f"{DIAGNOSTICS_DIRECTORY}/"
    if command("git", "check-ignore", "--quiet", entry) is not None:
        return False
    path = root / GITIGNORE
    existing = path.read_bytes() if path.exists() else b""
    newline = b"\r\n" if b"\r\n" in existing else b"\n"
    separator = newline if existing and not existing.endswith(b"\n") else b""
    path.write_bytes(existing + separator + entry.encode() + newline)
    return True


def check_prerequisites(command: CommandRunner, which: Callable[[str], str | None]) -> None:
    """Stop before any question when detection could not work; ralph-kit check repeats these on other machines."""
    problems = [
        f"{item.problem} → {item.fix}"
        for item in prerequisites(which, lambda executable: command(executable, "--version"))
        if item.problem
    ]
    if which("gh") and command("gh", "auth", "status") is None:
        problems.append("gh is not authenticated → gh auth login")
    if problems:
        raise InitError("missing prerequisites\n" + "\n".join(f"  ✗ {problem}" for problem in problems))


def run(
    root: pathlib.Path,
    prompter: Prompter,
    *,
    force: bool = False,
    config: pathlib.Path = pathlib.Path(CONFIG_NAME),
    command: CommandRunner | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> pathlib.Path:
    command = command or (lambda *args: run_command(*args, cwd=root))
    with prompter.working("Checking the prerequisites…"):
        check_prerequisites(command, which)
        toplevel = command("git", "rev-parse", "--show-toplevel")
    if toplevel is None or pathlib.Path(toplevel).resolve() != root:
        raise InitError("run ralph-kit init from the root of a Git repository")
    target = root / config  # An absolute config replaces root.
    display_path = target.relative_to(root) if target.is_relative_to(root) else target
    option_text = "" if target == root / CONFIG_NAME else f" --config {shlex.quote(str(display_path))}"
    if target.exists() and not force:
        raise InitError(f"{target} already exists: change it with `ralph-kit configure{option_text}`, or use --force to start over")
    if not target.parent.is_dir():
        raise InitError(f"{target.parent}/ does not exist")

    values = collect(root, prompter, command, which)
    contents = render(configuration(values, root), root)
    target.write_text(contents, encoding="utf-8")
    try:
        load_config(target, root)
    except RalphConfigError as error:  # The renderer must always produce a loadable file.
        raise InitError(f"generated configuration is invalid, please report this bug: {error}") from error
    ignored = ignore_diagnostics(root, command)

    prompter.say(f"\n✅ Wrote {target}.")
    if ignored:
        prompter.say(f"   Added {DIAGNOSTICS_DIRECTORY}/ to {GITIGNORE}: Ralph writes run diagnostics there.")
    if values["create_labels"]:
        create_offered_labels(prompter, command, values["repository"], values["create_labels"])
    remaining = [value for value in PLACEHOLDERS.values() if value in contents]
    if remaining:
        prompter.say("   Values to fill in: " + ", ".join(remaining))
    committed = f"{display_path} and {GITIGNORE}" if ignored else str(display_path)
    prompter.say(f"   Next step: commit {committed}, then run `ralph-kit check{option_text}`.")
    return target
