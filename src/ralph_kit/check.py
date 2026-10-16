"""Read-only readiness checks; never run agents, project checks, or GitHub writes."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys

from .config import RalphConfigError, load_config

PACKAGE_DIRECTORY = pathlib.Path(__file__).resolve().parent
AGENT_CLIS = {"claude": "Claude Code", "codex": "Codex"}


def placeholder(value):
    if isinstance(value, str):
        return any(marker in value for marker in ("VOTRE_", "MODELE_", "REMPLACER_"))
    if isinstance(value, dict):
        return any(placeholder(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(placeholder(item) for item in value)
    return False


def prerequisites(which, engines=None):
    """Tools init and check both need, as (name, problem or None) pairs.

    Without engines (init), any installed agent CLI is enough; otherwise each configured engine's CLI is required.
    """
    yield "Python 3.11+", None if sys.version_info >= (3, 11) else "Python 3.11 minimum"
    for executable in ("git", "gh"):
        yield f"CLI {executable}", None if which(executable) else f"{executable} absent du PATH"
    if engines is None:
        names = " ou ".join(f"{executable} ({name})" for executable, name in AGENT_CLIS.items())
        yield "CLI d'agent", None if any(which(executable) for executable in AGENT_CLIS) else f"aucun CLI d'agent dans le PATH : installer {names}"
    else:
        for executable in sorted(engines):
            yield f"CLI {executable}", None if which(executable) else f"{executable} absent du PATH"


class Readiness:
    def __init__(self, root):
        self.root = root
        self.errors = 0

    def check(self, name, action):
        try:
            action()
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError, ImportError, SyntaxError) as error:
            self.errors += 1
            print(f"❌ {name} : {error}")
        else:
            print(f"✅ {name}")

    def command(self, *args):
        try:
            result = subprocess.run(args, cwd=self.root, capture_output=True, text=True, timeout=30, env=os.environ | {"GIT_OPTIONAL_LOCKS": "0"})
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{args[0]} : délai de 30 secondes dépassé") from None
        if result.returncode:
            # CLI output can contain credentials; do not echo it.
            raise RuntimeError(f"{args[0]} {' '.join(args[1:])} a échoué ; vérifier l’accès et l’authentification")
        return result.stdout.strip()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_policy(config, root):
    spec = importlib.util.spec_from_file_location("ralph_readiness_quality", config.quality_module)
    require(spec is not None and spec.loader is not None, "module de vérification introuvable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("load_manifest", "matching_local_gates", "ci_requirements", "validate_check_evidence", "changed_paths"):
        require(callable(getattr(module, name, None)), f"fonction {name} manquante")
    manifest = module.load_manifest(config.quality_manifest)
    if config.local_checks:
        gates = [gate for gate in manifest["local_gates"] if gate.get("before_agent_review")]
        require(gates, "aucun contrôle local utilisable par la loop")
        for gate in gates:
            require(not placeholder(gate), "contrôle local encore à renseigner")
            command = gate.get("command")
            require(isinstance(command, list) and command and all(isinstance(arg, str) and arg for arg in command), "commande locale invalide")
            executable = command[0]
            available = shutil.which(executable) if "/" not in executable else shutil.which(str(root / executable))
            require(available, f"commande locale introuvable : {executable}")
    if config.ci_checks:
        checks = manifest["ci"]["checks"]
        require(checks, "aucun check CI configuré")
        names = [check["name"] for check in checks.values()]
        require(all(isinstance(name, str) and name.strip() for name in names), "nom de check CI invalide")
        require(not placeholder(checks), "checks CI encore à renseigner")
        require(len(set(names)) == len(names), "noms de checks CI dupliqués")
        module.ci_requirements(manifest, [], final=True)


def run(root, config_path, *, with_review=False):
    report = Readiness(root)
    try:
        config = load_config(config_path, root, require_review=with_review)
        roles = ("implementation", "correction", "review") if with_review else ("implementation", "correction")
        require(not placeholder([config.repository, config.trusted_authors, [config.models[role].name for role in roles]]), "repo, auteurs ou modèles encore à renseigner")
    except (RalphConfigError, ValueError) as error:
        print(f"❌ Configuration : {error}")
        return 1
    print("✅ Configuration et valeurs renseignées")
    engines = {config.models[role].engine for role in roles}
    for name, problem in prerequisites(shutil.which, engines):
        report.check(name, lambda problem=problem: require(problem is None, problem))

    def git_state():
        require(pathlib.Path(report.command("git", "rev-parse", "--show-toplevel")).resolve() == root, "lancer depuis la racine du repo cible")
        report.command("git", "check-ref-format", "--branch", config.base_branch)
        report.command("git", "remote", "get-url", config.remote)
        branch = report.command("git", "symbolic-ref", "--quiet", "--short", "HEAD")
        require(branch != config.base_branch, "créer une branche dédiée différente de la branche de base")
        require(not report.command("git", "status", "--porcelain"), "arbre Git non propre : terminer les changements avant lancement")
    report.check("Repo Git, remote, branche dédiée et arbre propre", git_state)

    def github():
        # Default repo must match, because the runner uses gh repo view the same way.
        repo = json.loads(report.command("gh", "repo", "view", "--json", "nameWithOwner"))
        require(repo["nameWithOwner"] == config.repository, "le repo détecté par gh diffère du TOML")
        user = json.loads(report.command("gh", "api", "user"))["login"]
        require(user in config.trusted_authors, "ajouter le compte gh courant aux trusted_authors pour les reprises")
        details = json.loads(report.command("gh", "api", f"repos/{config.repository}"))
        require(details.get("permissions", {}).get("push"), "le compte gh n’a pas l’accès en écriture au repo")
        labels = json.loads(report.command("gh", "api", "--paginate", "--slurp", f"repos/{config.repository}/labels"))
        available = {label["name"] for page in labels for label in page}
        missing = set(config.labels.values()) - available
        require(not missing, "labels GitHub manquants : " + ", ".join(sorted(missing)))
    report.check("Accès GitHub, compte de confiance et labels", github)

    for engine in sorted(engines):
        directory = ".claude/skills" if engine == "claude" else ".agents/skills"
        report.check(f"Skill implement pour {engine}", lambda directory=directory: require((root / directory / "implement" / "SKILL.md").is_file(), f"{directory}/implement/SKILL.md absent ou lien cassé"))
    def delivery_files():
        schema = json.loads((PACKAGE_DIRECTORY / "outcome-schema.json").read_text())
        require(isinstance(schema, dict) and schema.get("type") == "object", "schéma de résultat invalide")
    report.check("Fichiers de ralph-kit", delivery_files)
    if config.verification.needs_quality_policy:
        report.check("Module et contrôles activés (sans les exécuter)", lambda: check_policy(config.verification, root))
    else:
        print("ℹ️ Contrôles locaux et CI désactivés ; module et manifeste non chargés.")
    if report.errors:
        noun = "blocage" if report.errors == 1 else "blocages"
        print(f"\n❌ {report.errors} {noun} à corriger. Relancez le précontrôle ensuite.")
    else:
        print("\n✅ Précontrôle réussi.")
    print("\n📋 À vérifier de votre côté avant de lancer :")
    print("  ☐ Connecter les agents utilisés sur cette machine et confirmer l’accès aux modèles choisis.")
    print("  ☐ Vérifier vos permissions et identifiants Git pour pousser la branche.")
    print("  ☐ Installer les dépendances des skills et vérifier leur découverte par chaque agent.")
    print("  ☐ Installer les outils, dépendances et services nécessaires au projet.")
    if config.verification.local_checks:
        print("  ☐ Exécuter les commandes de contrôles locaux et vérifier leurs résultats.")
    if config.verification.ci_checks:
        print("  ☐ Confirmer les noms des checks dans le workflow CI et vérifier qu’il fonctionne.")
    print("  ☐ Relire la spec, les critères d’acceptation, les sous-issues, leurs labels et dépendances GitHub.")
    print("  ☐ Accepter l’accès autonome des agents : la loop contourne les approbations et le sandbox.")
    return int(bool(report.errors))


def main(argv=None, prog=None):
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path("ralph.toml"))
    parser.add_argument("--review", "--with-review", dest="with_review", action="store_true", help="vérifier aussi le rôle de review indépendante")
    args = parser.parse_args(argv)
    root = pathlib.Path.cwd().resolve()
    path = args.config if args.config.is_absolute() else root / args.config
    return run(root, path, with_review=args.with_review)
