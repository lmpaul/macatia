"""Read-only readiness checks; never run agents, project checks, or GitHub writes."""

from __future__ import annotations

import argparse
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

from . import __version__
from .config import MissingConfigError, RalphConfigError, load_config

PACKAGE_DIRECTORY = pathlib.Path(__file__).resolve().parent
AGENT_CLIS = {"claude": "Claude Code", "codex": "Codex"}
INSTALL_HINTS = {
    "git": "installer git",
    "gh": "installer GitHub CLI : https://cli.github.com",
    "claude": "installer Claude Code (commande claude)",
    "codex": "installer Codex CLI (commande codex)",
}
REMINDERS = (
    "Agents connectés sur cette machine et accès aux modèles choisis",
    "Permissions et identifiants Git pour pousser la branche",
    "Dépendances des skills installées et skills découvertes par chaque agent",
    "Outils, dépendances et services nécessaires au projet",
)
FINAL_REMINDERS = (
    "Spec, critères d’acceptation, sous-issues, labels et dépendances GitHub relus",
    "Accès autonome des agents accepté : la loop contourne les approbations et le sandbox",
)
ERRORS = (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError, ImportError, SyntaxError)


def placeholder(value):
    if isinstance(value, str):
        return any(marker in value for marker in ("VOTRE_", "MODELE_", "REMPLACER_"))
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


def prerequisites(which, engines=None):
    """Tools init and check both need.

    Without engines (init), any installed agent CLI is enough; otherwise each configured engine's CLI is required.
    """
    version = ".".join(map(str, sys.version_info[:3]))
    yield Prerequisite("Python", version, None if sys.version_info >= (3, 11) else f"Python {version}, 3.11 minimum", "installer Python 3.11 ou plus récent")

    def tool(executable):
        path = which(executable)
        return Prerequisite(executable, path or "", None if path else f"{executable} absent du PATH", INSTALL_HINTS[executable])

    yield tool("git")
    yield tool("gh")
    if engines is None:
        found = [executable for executable in AGENT_CLIS if which(executable)]
        names = " ou ".join(f"{executable} ({name})" for executable, name in AGENT_CLIS.items())
        yield Prerequisite("CLI d'agent", ", ".join(found), None if found else "aucun CLI d'agent dans le PATH", f"installer {names}")
    else:
        for executable in sorted(engines):
            yield tool(executable)


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
    status: str  # ok, fail, skip (not verified) or off (disabled)
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
        self.add("skip", label, f"non vérifié : {reason}")

    def command(self, *args):
        try:
            result = subprocess.run(args, cwd=self.root, capture_output=True, text=True, timeout=30, env=os.environ | {"GIT_OPTIONAL_LOCKS": "0"})
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{args[0]} : délai de 30 secondes dépassé") from None
        if result.returncode:
            # CLI output can contain credentials; do not echo it.
            raise RuntimeError(f"{args[0]} {' '.join(args[1:])} a échoué ; vérifier l’accès et l’authentification")
        return result.stdout.strip()


ICONS = {"ok": ("✓", "green"), "fail": ("✗", "bold red"), "skip": ("○", "yellow"), "off": ("–", "dim")}


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
        summary.append(f"✗ {counts['fail']} {'bloquant' if counts['fail'] == 1 else 'bloquants'}", style="bold red")
    else:
        summary.append("✓ Aucun bloquant", style="bold green")
    summary.append(f" · {counts['ok']} OK", style="dim")
    if counts["skip"]:
        summary.append(f" · {counts['skip']} non vérifié{'s' if counts['skip'] > 1 else ''}", style="dim")
    console.print(summary)
    if counts["fail"]:
        console.print("Corriger les points en rouge, puis relancer ralph-kit check.", style="dim")
    if reminders:
        console.print()
        console.print("Avant de lancer, à vérifier vous-même :", style="bold")
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=1)
        grid.add_column()
        for reminder in reminders:
            grid.add_row("·", reminder, style="dim")
        console.print(Padding(grid, (0, 0, 0, 2)))


def check_policy(config, root):
    spec = importlib.util.spec_from_file_location("ralph_readiness_quality", config.quality_module)
    require(spec is not None and spec.loader is not None, "module de vérification introuvable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("load_manifest", "matching_local_gates", "ci_requirements", "validate_check_evidence", "changed_paths"):
        require(callable(getattr(module, name, None)), f"fonction {name} manquante")
    manifest = module.load_manifest(config.quality_manifest)
    found = []
    if config.local_checks:
        gates = [gate for gate in manifest["local_gates"] if gate.get("before_agent_review")]
        require(gates, "aucun contrôle local utilisable par la loop", "ajouter un contrôle avec before_agent_review au manifeste")
        for gate in gates:
            require(not placeholder(gate), "contrôle local encore à renseigner", "compléter la commande dans le manifeste")
            command = gate.get("command")
            require(isinstance(command, list) and command and all(isinstance(arg, str) and arg for arg in command), "commande locale invalide")
            executable = command[0]
            available = shutil.which(executable) if "/" not in executable else shutil.which(str(root / executable))
            require(available, f"commande locale introuvable : {executable}", f"installer {executable} ou corriger la commande")
        found.append(f"{len(gates)} contrôle{'s' if len(gates) > 1 else ''} local{'aux' if len(gates) > 1 else ''}")
    if config.ci_checks:
        checks = manifest["ci"]["checks"]
        require(checks, "aucun check CI configuré")
        names = [check["name"] for check in checks.values()]
        require(all(isinstance(name, str) and name.strip() for name in names), "nom de check CI invalide")
        require(not placeholder(checks), "checks CI encore à renseigner", "compléter les noms des checks dans le manifeste")
        require(len(set(names)) == len(names), "noms de checks CI dupliqués")
        module.ci_requirements(manifest, [], final=True)
        found.append(f"{len(names)} check{'s' if len(names) > 1 else ''} CI")
    return ", ".join(found) + " (non exécutés)"


def run(root, config_path, *, with_review=False, console=None):
    console = console or Console(highlight=False)
    report = Readiness(root)
    report.section("Configuration")
    try:
        config = load_config(config_path, root, require_review=with_review)
        roles = ("implementation", "correction", "review") if with_review else ("implementation", "correction")
        require(not placeholder([config.repository, config.trusted_authors, [config.models[role].name for role in roles]]), "repo, auteurs ou modèles encore à renseigner")
    except MissingConfigError:
        report.add("fail", config_path.name, "introuvable : ce repo n’a pas encore été configuré", "ralph-kit init")
        render(console, report)
        return 1
    except (RalphConfigError, ValueError) as error:
        report.add("fail", config_path.name, str(error), f"compléter {config_path.name}, ou le régénérer avec ralph-kit init --force")
        render(console, report)
        return 1
    report.add("ok", config_path.name, "valeurs renseignées")

    report.section("Outils")
    engines = {config.models[role].engine for role in roles}
    available = set()
    for item in prerequisites(shutil.which, engines):
        if report.add("fail" if item.problem else "ok", item.name, item.problem or item.detail, item.fix if item.problem else None):
            available.add(item.name)

    def package_files():
        schema = json.loads((PACKAGE_DIRECTORY / "outcome-schema.json").read_text())
        require(isinstance(schema, dict) and schema.get("type") == "object", "schéma de résultat invalide")
        return f"{__version__}, fichiers complets"
    report.check("ralph-kit", package_files, "réinstaller ralph-kit")

    report.section("Git")
    if "git" not in available:
        for label in ("Racine du repo", "Remote", "Branche", "Arbre de travail"):
            report.skip(label, "git absent")
    else:
        def repository_root():
            toplevel = pathlib.Path(report.command("git", "rev-parse", "--show-toplevel")).resolve()
            require(toplevel == root, f"lancé depuis un sous-dossier de {toplevel}", f"cd {shlex.quote(str(toplevel))}")
            return str(root)
        report.check("Racine du repo", repository_root)

        def remote():
            url = report.command("git", "remote", "get-url", config.remote)
            return f"{config.remote} → {re.sub(r'//[^/@]+@', '//', url)}"  # Hide credentials embedded in HTTPS URLs.
        report.check("Remote", remote, f"git remote add {config.remote} <url>, ou corriger remote dans ralph.toml")

        def branch():
            report.command("git", "check-ref-format", "--branch", config.base_branch)
            try:
                current = report.command("git", "symbolic-ref", "--quiet", "--short", "HEAD")
            except RuntimeError:
                raise Blocked("HEAD détaché, aucune branche active", "git switch -c <nom-de-branche>") from None
            require(current != config.base_branch, f"sur la branche de base {current}", "git switch -c <nom-de-branche>")
            return f"{current} (base : {config.base_branch})"
        report.check("Branche", branch)

        def worktree():
            changes = report.command("git", "status", "--porcelain").splitlines()
            count = len(changes)
            require(not changes, f"{count} fichier{'s' if count > 1 else ''} modifié{'s' if count > 1 else ''} ou non suivi{'s' if count > 1 else ''}", "commiter ou remiser (git stash) les changements")
            return "propre"
        report.check("Arbre de travail", worktree)

    report.section("GitHub")
    if "gh" not in available:
        for label in ("Repo", "Compte gh", "Compte de confiance", "Accès en écriture", "Labels"):
            report.skip(label, "gh absent")
    else:
        def repository():
            # Default repo must match, because the runner uses gh repo view the same way.
            detected = json.loads(report.command("gh", "repo", "view", "--json", "nameWithOwner"))["nameWithOwner"]
            require(detected == config.repository, f"gh détecte {detected}, ralph.toml indique {config.repository}", f"corriger repository dans ralph.toml, ou gh repo set-default {config.repository}")
            return config.repository
        report.check("Repo", repository)

        login = None

        def account():
            nonlocal login
            login = json.loads(report.command("gh", "api", "user"))["login"]
            return login
        if report.check("Compte gh", account, "gh auth login"):
            report.check("Compte de confiance", lambda: require(login in config.trusted_authors, f"{login} absent de trusted_authors", f"ajouter {login} à trusted_authors dans ralph.toml") or "dans trusted_authors")

            def write_access():
                details = json.loads(report.command("gh", "api", f"repos/{config.repository}"))
                require(details.get("permissions", {}).get("push"), f"{login} n’a pas l’accès en écriture", f"demander l’accès en écriture à {config.repository}")
                return "push autorisé"
            report.check("Accès en écriture", write_access)

            def labels():
                pages = json.loads(report.command("gh", "api", "--paginate", "--slurp", f"repos/{config.repository}/labels"))
                present = {label["name"] for page in pages for label in page}
                wanted = set(config.labels.values())
                missing = sorted(wanted - present)
                names = " ".join(shlex.quote(name) for name in missing)
                fix = f"gh label create {names}" if len(missing) == 1 else f'for label in {names}; do gh label create "$label"; done'
                require(not missing, f"{len(missing)} manquant{'s' if len(missing) > 1 else ''} : {', '.join(missing)}", fix)
                return f"{len(wanted)} présents"
            report.check("Labels", labels)
        else:
            for label in ("Compte de confiance", "Accès en écriture", "Labels"):
                report.skip(label, "compte gh indisponible")

    report.section("Agents")
    for engine in sorted(engines):
        directory = ".claude/skills" if engine == "claude" else ".agents/skills"
        path = f"{directory}/implement/SKILL.md"
        report.check(f"Skill implement ({engine})", lambda path=path: require((root / path).is_file(), f"{path} absent ou lien cassé", f"ajouter la skill implement dans {directory}/") or path)

    report.section("Contrôles qualité")
    if config.verification.needs_quality_policy:
        report.check("Module et manifeste", lambda: check_policy(config.verification, root))
    else:
        report.add("off", "Contrôles", "locaux et CI désactivés dans ralph.toml")

    reminders = list(REMINDERS)
    if config.verification.local_checks:
        reminders.append("Commandes de contrôles locaux exécutées et résultats vérifiés")
    if config.verification.ci_checks:
        reminders.append("Noms des checks du workflow CI confirmés et workflow fonctionnel")
    render(console, report, config.repository, [*reminders, *FINAL_REMINDERS])
    return int(bool(report.errors))


def main(argv=None, prog=None):
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path("ralph.toml"))
    parser.add_argument("--review", "--with-review", dest="with_review", action="store_true", help="vérifier aussi le rôle de review indépendante")
    args = parser.parse_args(argv)
    root = pathlib.Path.cwd().resolve()
    path = args.config if args.config.is_absolute() else root / args.config
    return run(root, path, with_review=args.with_review)
