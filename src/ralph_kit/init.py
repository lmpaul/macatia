"""`ralph-kit init`: detect what can be read locally, ask for the rest, write ralph.toml."""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import TextIO

from .check import AGENT_CLIS as ENGINES, create_labels, missing_labels, prerequisites
from .config import (
    CLAUDE_REASONING_EFFORTS,
    DIAGNOSTICS_DIRECTORY,
    RalphConfigError,
    load_config,
)


CONFIG_NAME = "ralph.toml"
GITIGNORE = ".gitignore"
QUALITY_MODULE = ".github/scripts/quality.py"
QUALITY_MANIFEST = ".github/quality-gates.json"
LABELS = {
    "needs_triage": "needs-triage",
    "needs_info": "needs-info",
    "ready_for_agent": "ready-for-agent",
    "ready_for_human": "ready-for-human",
    "wontfix": "wontfix",
    "human_review": "human-review",
    "implemented": "implemented",
}
# Claude Code has no command listing models; its documented aliases follow the latest version.
CLAUDE_MODELS = [
    ("fable", "Fable, for the hardest and longest tasks"),
    ("opus", "latest Opus, complex reasoning"),
    ("sonnet", "latest Sonnet, everyday coding"),
    ("haiku", "latest Haiku, fast and cheap"),
]
DEFAULT_MODELS = {"claude": {"implementation": "sonnet", "review": "opus"}}
EFFORTS = {
    "claude": sorted(CLAUDE_REASONING_EFFORTS, key=["low", "medium", "high", "xhigh", "max"].index),
    "codex": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
}
OTHER = "other"
PLACEHOLDERS = {
    "repository": "YOUR_ORG/YOUR_REPO",
    "author": "YOUR_GITHUB_ACCOUNT",
    "implementation": "CHOOSE_IMPLEMENTATION_MODEL",
    "review": "CHOOSE_REVIEW_MODEL",
}

CommandRunner = Callable[..., "str | None"]
Selector = Callable[[str, "list[tuple[str, str]]", str], str]


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
    ):
        self.interactive = interactive
        self.read = read
        self.output = output
        self.select = select

    def say(self, message: str) -> None:
        print(message, file=self.output)

    def _read(self, prompt: str) -> str:
        try:
            return self.read(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise InitAborted("init cancelled, nothing written") from None

    def choice(self, question: str, options: list[tuple[str, str]], default: str) -> str:
        if not self.interactive:
            return default
        if self.select:
            try:
                return self.select(question, options, default)
            except KeyboardInterrupt:
                raise InitAborted("init cancelled, nothing written") from None
        # Numbered fallback when no arrow-key selector is available.
        values = [value for value, _ in options]
        self.say(f"\n? {question}")
        for index, (value, label) in enumerate(options, start=1):
            self.say(f"  {index}) {value}{f'  ({label})' if label else ''}")
        default_index = values.index(default) + 1
        while True:
            answer = self._read(f"Choice [{default_index}]: ")
            if not answer:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(values):
                return values[int(answer) - 1]
            if answer in values:
                return answer
            self.say(f"  Expected a number between 1 and {len(values)}.")

    def text(
        self,
        question: str,
        *,
        default: str | None = None,
        placeholder: str | None = None,
        validate: Callable[[str], str | None] | None = None,
    ) -> str:
        if not self.interactive:
            return placeholder if placeholder is not None else default or ""
        self.say(f"\n? {question}")
        while True:
            answer = self._read(f"Answer [{default}]: " if default else "Answer: ") or (default or "")
            error = "an answer is required" if not answer else (validate(answer) if validate else None)
            if error is None:
                return answer
            self.say(f"  {error}")


def _repository_error(value: str) -> str | None:
    return None if re.fullmatch(r"[^/\s]+/[^/\s]+", value) else "expected format: owner/name"


def _git_name_error(value: str) -> str | None:
    if value.startswith("-") or any(character.isspace() for character in value):
        return "invalid Git name (no spaces, must not start with -)"
    return None


def _authors(value: str) -> list[str]:
    return list(dict.fromkeys(author.strip() for author in value.split(",") if author.strip()))


def _detect_repository(command: CommandRunner) -> tuple[str | None, str | None]:
    output = command("gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef")
    if not output:
        return None, None
    try:
        details = json.loads(output)
    except json.JSONDecodeError:
        return None, None
    branch = (details.get("defaultBranchRef") or {}).get("name") or None
    return details.get("nameWithOwner") or None, branch


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


def _choose_model(prompter: Prompter, engine: str, catalog: list[dict], role: str, question: str) -> str:
    if engine == "claude":
        options = CLAUDE_MODELS
        default = DEFAULT_MODELS["claude"][role]
    else:
        options = [(model["name"], model["description"]) for model in catalog]
        default = options[0][0] if options else None
    if not options:
        return prompter.text(f"{question} (Codex model list unavailable)", placeholder=PLACEHOLDERS[role])
    if not prompter.interactive:
        return PLACEHOLDERS[role]
    name = prompter.choice(question, [*options, (OTHER, "enter a full model name")], default)
    if name == OTHER:
        name = prompter.text("Model name", placeholder=PLACEHOLDERS[role])
    return name


def _model_efforts(engine: str, catalog: list[dict], name: str) -> list[str]:
    model = next((model for model in catalog if model["name"] == name), None)
    supported = [effort for effort in model["efforts"] if effort in EFFORTS[engine]] if model else []
    return supported or EFFORTS[engine]


def collect(root: pathlib.Path, prompter: Prompter, command: CommandRunner, which: Callable[[str], str | None]) -> dict:
    say = prompter.say
    say("🔎 Detecting from git and gh…")

    repository, base_branch = _detect_repository(command)
    if repository:
        say(f"  ✓ GitHub repository: {repository}")
    else:
        repository = prompter.text(
            "GitHub repository (owner/name); gh could not detect it",
            placeholder=PLACEHOLDERS["repository"],
            validate=_repository_error,
        )

    if not base_branch:
        remote_head = command("git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
        base_branch = remote_head.split("/", 1)[1] if remote_head and "/" in remote_head else None
    if base_branch:
        say(f"  ✓ Base branch: {base_branch}")
    else:
        base_branch = prompter.text("Base branch (Ralph never commits to it)", default="main", validate=_git_name_error)

    remotes = (command("git", "remote") or "").split()
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
        remote = prompter.text("Remote Ralph pushes to (no Git remote found)", default="origin", validate=_git_name_error)

    login = command("gh", "api", "user", "--jq", ".login")
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
    catalog = codex_catalog(command) if engine == "codex" else []
    models = {}
    for role, question, effort in (
        ("implementation", "Model for implementation and corrections", "medium"),
        ("review", "Model for the independent review (ralph-kit run --review)", "high"),
    ):
        name = _choose_model(prompter, engine, catalog, role, question)
        efforts = _model_efforts(engine, catalog, name)
        models[role] = {
            "engine": engine,
            "name": name,
            "display_name": name,
            "reasoning_effort": prompter.choice(
                f"Reasoning effort for {name}",
                [(level, "") for level in efforts],
                effort if effort in efforts else efforts[len(efforts) // 2],
            ),
        }
    models["correction"] = dict(models["implementation"])

    return {
        "create_labels": _offer_labels(prompter, command, repository),
        "repository": repository,
        "base_branch": base_branch,
        "remote": remote,
        "trusted_authors": authors,
        "models": models,
        "local_checks": checks,
        "ci_checks": checks,
    }


def _offer_labels(prompter: Prompter, command: CommandRunner, repository: str) -> dict[str, str]:
    """Missing labels the user agreed to create once ralph.toml is written."""
    if repository == PLACEHOLDERS["repository"]:
        return {}
    try:
        missing = missing_labels(command, repository, LABELS.values())
    except (TypeError, KeyError, ValueError):  # gh unavailable or unexpected output: ralph-kit check reports it.
        return {}
    if not missing:
        prompter.say(f"  ✓ Labels: all {len(LABELS)} present")
        return {}
    prompter.say(f"  ℹ Labels missing on {repository}: {', '.join(missing)}")
    if not prompter.interactive:
        return {}
    answer = prompter.choice(
        f"Create the {len(missing)} missing label{'s' if len(missing) > 1 else ''}?",
        [("Yes", "create them now with gh"), ("Later", "ralph-kit check will offer again")],
        "Yes",
    )
    return {role: name for role, name in LABELS.items() if name in missing} if answer == "Yes" else {}


def _string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render(values: dict) -> str:
    lines = [
        "# Ralph configuration for this repository. See https://github.com/lmpaul/ralph-kit",
        "version = 1",
        f"repository = {_string(values['repository'])}",
        f"base_branch = {_string(values['base_branch'])}",
        f"remote = {_string(values['remote'])}",
        f"trusted_authors = [{', '.join(_string(author) for author in values['trusted_authors'])}]",
        "",
        "[labels]",
        *(f"{key} = {_string(label)}" for key, label in LABELS.items()),
    ]
    for role in ("implementation", "review", "correction"):
        profile = values["models"][role]
        lines.append("")
        if role == "review":
            lines.append("# Only used by `ralph-kit run --review`.")
        lines.append(f"[models.{role}]")
        lines += [f"{key} = {_string(profile[key])}" for key in ("engine", "name", "display_name", "reasoning_effort")]
    lines += [
        "",
        "[verification]",
        "# Both checks need the quality module and manifest below; disabled checks do not load them.",
        f"local_checks = {str(values['local_checks']).lower()}",
        f"ci_checks = {str(values['ci_checks']).lower()}",
        f"quality_module = {_string(QUALITY_MODULE)}",
        f"quality_manifest = {_string(QUALITY_MANIFEST)}",
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
    command: CommandRunner | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> pathlib.Path:
    command = command or (lambda *args: run_command(*args, cwd=root))
    check_prerequisites(command, which)
    toplevel = command("git", "rev-parse", "--show-toplevel")
    if toplevel is None or pathlib.Path(toplevel).resolve() != root:
        raise InitError("run ralph-kit init from the root of a Git repository")
    target = root / CONFIG_NAME
    if target.exists() and not force:
        raise InitError(f"{target} already exists (use --force to overwrite it)")

    values = collect(root, prompter, command, which)
    contents = render(values)
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
        failed = create_labels(command, values["repository"], values["create_labels"])
        for name in values["create_labels"].values():
            prompter.say(f"   ✗ Label {name} not created" if name in failed else f"   ✓ Label {name} created")
    remaining = [value for value in PLACEHOLDERS.values() if value in contents]
    if remaining:
        prompter.say("   Values to fill in: " + ", ".join(remaining))
    committed = f"{CONFIG_NAME} and {GITIGNORE}" if ignored else CONFIG_NAME
    prompter.say(f"   Next step: commit {committed}, then run `ralph-kit check`.")
    return target
