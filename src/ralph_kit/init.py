"""`ralph-kit init`: detect what can be read locally, ask for the rest, write ralph.toml."""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Callable, TextIO

from .config import CLAUDE_REASONING_EFFORTS, RalphConfigError, load_config

CONFIG_NAME = "ralph.toml"
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
ENGINES = ("claude", "codex")
DEFAULT_MODELS = {
    "claude": {"implementation": "sonnet", "review": "opus"},
    "codex": {"implementation": "gpt-5.6-terra", "review": "gpt-5.6-sol"},
}
EFFORTS = {
    "claude": sorted(CLAUDE_REASONING_EFFORTS, key=["low", "medium", "high", "xhigh", "max"].index),
    "codex": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
}
PLACEHOLDERS = {
    "repository": "VOTRE_ORG/VOTRE_REPO",
    "author": "VOTRE_COMPTE_GITHUB",
    "implementation": "MODELE_IMPLEMENTATION",
    "review": "MODELE_REVIEW",
}

CommandRunner = Callable[..., "str | None"]


class InitError(RuntimeError):
    """init cannot write a configuration in this directory."""


class InitAborted(RuntimeError):
    """The user interrupted the questions; nothing was written."""


def run_command(*args: str, cwd: pathlib.Path) -> str | None:
    """Read-only probe: stdout on success, None when the tool is missing or fails."""
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


class Prompter:
    """Ask questions on a terminal; without one, keep defaults and placeholders."""

    def __init__(self, interactive: bool, read: Callable[[str], str] = input, output: TextIO = sys.stdout):
        self.interactive = interactive
        self.read = read
        self.output = output

    def say(self, message: str) -> None:
        print(message, file=self.output)

    def _read(self, prompt: str) -> str:
        try:
            return self.read(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise InitAborted("init annulé, aucun fichier écrit") from None

    def choice(self, question: str, options: list[tuple[str, str]], default: str) -> str:
        if not self.interactive:
            return default
        values = [value for value, _ in options]
        self.say(f"\n? {question}")
        for index, (value, label) in enumerate(options, start=1):
            self.say(f"  {index}) {value}{f'  ({label})' if label else ''}")
        default_index = values.index(default) + 1
        while True:
            answer = self._read(f"Choix [{default_index}] : ")
            if not answer:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(values):
                return values[int(answer) - 1]
            if answer in values:
                return answer
            self.say(f"  Réponse attendue : un numéro entre 1 et {len(values)}.")

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
            answer = self._read(f"Réponse [{default}] : " if default else "Réponse : ") or (default or "")
            error = "réponse obligatoire" if not answer else (validate(answer) if validate else None)
            if error is None:
                return answer
            self.say(f"  {error}")


def _repository_error(value: str) -> str | None:
    return None if re.fullmatch(r"[^/\s]+/[^/\s]+", value) else "format attendu : owner/name"


def _git_name_error(value: str) -> str | None:
    if value.startswith("-") or any(character.isspace() for character in value):
        return "nom Git invalide (pas d'espace, ne commence pas par -)"
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


def collect(root: pathlib.Path, prompter: Prompter, command: CommandRunner, which: Callable[[str], str | None]) -> dict:
    say = prompter.say
    say("🔎 Détection depuis git et gh…")

    repository, base_branch = _detect_repository(command)
    if repository:
        say(f"  ✓ Repo GitHub : {repository}")
    else:
        repository = prompter.text(
            "Repo GitHub (owner/name) — gh n'a pas pu le détecter",
            placeholder=PLACEHOLDERS["repository"],
            validate=_repository_error,
        )

    if not base_branch:
        remote_head = command("git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
        base_branch = remote_head.split("/", 1)[1] if remote_head and "/" in remote_head else None
    if base_branch:
        say(f"  ✓ Branche de base : {base_branch}")
    else:
        base_branch = prompter.text("Branche de base (Ralph n'y committe jamais)", default="main", validate=_git_name_error)

    remotes = (command("git", "remote") or "").split()
    if len(remotes) == 1:
        remote = remotes[0]
        say(f"  ✓ Remote : {remote}")
    elif remotes:
        remote = prompter.choice(
            "Remote sur lequel Ralph pousse",
            [(name, command("git", "remote", "get-url", name) or "") for name in remotes],
            "origin" if "origin" in remotes else remotes[0],
        )
    else:
        remote = prompter.text("Remote sur lequel Ralph pousse — aucun remote Git trouvé", default="origin", validate=_git_name_error)

    login = command("gh", "api", "user", "--jq", ".login")
    if login:
        authors = [login]
        say(f"  ✓ Auteur de confiance : {login} (compte gh courant)")
    else:
        authors = _authors(prompter.text(
            "Comptes GitHub de confiance, séparés par des virgules (inclure celui de la loop)",
            placeholder=PLACEHOLDERS["author"],
            validate=lambda value: None if _authors(value) else "au moins un compte",
        ))

    checks = (root / QUALITY_MODULE).is_file() and (root / QUALITY_MANIFEST).is_file()
    if checks:
        say(f"  ✓ Module qualité trouvé : contrôles locaux et CI activés ({QUALITY_MODULE})")
    else:
        say(f"  ℹ Pas de {QUALITY_MODULE} + {QUALITY_MANIFEST} : contrôles locaux et CI désactivés")

    installed = [engine for engine in ENGINES if which(engine)]
    engine = prompter.choice(
        "Moteur des agents",
        [(name, "installé" if name in installed else "absent du PATH") for name in ENGINES],
        installed[0] if installed else ENGINES[0],
    )
    efforts = [(effort, "") for effort in EFFORTS[engine]]
    models = {}
    for role, question, effort in (
        ("implementation", "Modèle pour l'implémentation et les corrections", "medium"),
        ("review", "Modèle pour la review indépendante (ralph-kit run --review)", "high"),
    ):
        name = prompter.text(question, default=DEFAULT_MODELS[engine][role], placeholder=PLACEHOLDERS[role])
        models[role] = {
            "engine": engine,
            "name": name,
            "display_name": name,
            "reasoning_effort": prompter.choice(f"Effort de raisonnement pour {name}", efforts, effort),
        }
    models["correction"] = dict(models["implementation"])

    return {
        "repository": repository,
        "base_branch": base_branch,
        "remote": remote,
        "trusted_authors": authors,
        "models": models,
        "local_checks": checks,
        "ci_checks": checks,
    }


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


def run(
    root: pathlib.Path,
    prompter: Prompter,
    *,
    force: bool = False,
    command: CommandRunner | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> pathlib.Path:
    command = command or (lambda *args: run_command(*args, cwd=root))
    toplevel = command("git", "rev-parse", "--show-toplevel")
    if toplevel is None or pathlib.Path(toplevel).resolve() != root:
        raise InitError("lancer ralph-kit init depuis la racine d'un repo Git")
    target = root / CONFIG_NAME
    if target.exists() and not force:
        raise InitError(f"{target} existe déjà (--force pour l'écraser)")

    values = collect(root, prompter, command, which)
    contents = render(values)
    target.write_text(contents, encoding="utf-8")
    try:
        load_config(target, root)
    except RalphConfigError as error:  # The renderer must always produce a loadable file.
        raise InitError(f"configuration générée invalide, merci de signaler ce bug : {error}") from error

    prompter.say(f"\n✅ {target} écrit.")
    remaining = [value for value in PLACEHOLDERS.values() if value in contents]
    if remaining:
        prompter.say("   Valeurs à compléter : " + ", ".join(remaining))
    prompter.say("   Étape suivante : commiter ralph.toml, puis lancer `ralph-kit check`.")
    return target
