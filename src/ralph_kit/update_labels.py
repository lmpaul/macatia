"""Complete [labels] in the configuration with the default names, then offer the missing labels on GitHub.

The configuration is edited in place, one line per missing role, so its layout and comments stay.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shlex
import sys
import tomllib

from .check import Readiness, confirm_with_arrows, create_labels, missing_labels
from .config import DEFAULT_LABELS, RalphConfigError, _load_toml, load_config


TABLE_HEADER = re.compile(r"\s*\[")
LABELS_HEADER = re.compile(r"\s*\[\s*labels\s*\]\s*(#.*)?")


class UpdateError(RuntimeError):
    """The configuration cannot be completed safely; nothing was written."""


def missing_roles(labels: dict) -> dict[str, str]:
    """The {role: default name} of each role the table leaves out, in the order init writes them."""
    missing = {role: name for role, name in DEFAULT_LABELS.items() if role not in labels}
    taken = {name: role for role, name in labels.items() if isinstance(name, str)}
    for role, name in missing.items():
        if name in taken:
            raise UpdateError(
                f"{name} is already the label of {taken[name]}: add {role} under [labels] by hand, with another name"
            )
    return missing


def with_roles(text: str, roles: dict[str, str]) -> str:
    """text with one line per role at the end of its [labels] table, after the last non-blank line."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    start = next((index for index, line in enumerate(lines) if LABELS_HEADER.fullmatch(line.rstrip("\n"))), None)
    if start is None:
        raise UpdateError("no [labels] table to complete: add the missing roles by hand")
    end = next((index for index in range(start + 1, len(lines)) if TABLE_HEADER.match(lines[index])), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    added = [f"{role} = {json.dumps(name, ensure_ascii=False)}\n" for role, name in roles.items()]
    return "".join(lines[:end] + added + lines[end:])


def complete_file(path: pathlib.Path) -> dict[str, str]:
    """Add the missing roles to the file; return them, empty when it already declares every role."""
    values = _load_toml(path)
    labels = values.get("labels")
    if not isinstance(labels, dict):
        raise UpdateError("no [labels] table to complete: add the missing roles by hand")
    roles = missing_roles(labels)
    if roles:
        text = with_roles(path.read_text(encoding="utf-8"), roles)
        if not set(DEFAULT_LABELS) <= set(tomllib.loads(text).get("labels", {})):
            raise UpdateError("no [labels] table to complete: add the missing roles by hand")
        path.write_text(text, encoding="utf-8")
    return roles


def run(root: pathlib.Path, config_path: pathlib.Path, *, confirm=None, command=None) -> int:
    """confirm(question) -> bool offers to create missing labels; None never writes to GitHub.

    command(*args) -> stdout runs gh; it raises RuntimeError on failure.
    """
    command = command or Readiness(root).command
    name = config_path.name
    try:
        roles = complete_file(config_path)
        if roles:
            for role, label in roles.items():
                print(f'✓ {name}: added {role} = "{label}"')
        else:
            print(f"✓ {name} declares all {len(DEFAULT_LABELS)} label roles")
        config = load_config(config_path, root, require_review=False)
    except (UpdateError, RalphConfigError, tomllib.TOMLDecodeError) as error:
        print(f"❌ {error}", file=sys.stderr)
        return 1

    code = 0
    repository = config.repository
    try:
        missing = missing_labels(command, repository, config.labels.values())
    except (RuntimeError, ValueError) as error:
        print(f"❌ could not list the labels of {repository}: {error}", file=sys.stderr)
        missing, code = [], 1
    if not missing and not code:
        print(f"✓ all {len(config.labels)} labels present on {repository}")
    elif missing:
        print(f"ℹ {len(missing)} missing on {repository}: {', '.join(missing)}")
        count = f"{len(missing)} missing label{'s' if len(missing) > 1 else ''}"
        if confirm and confirm(f"Create the {count} on {repository}?"):
            roles_by_name = {label: role for role, label in config.labels.items()}
            failed = create_labels(command, repository, {roles_by_name[label]: label for label in missing})
            for label in missing:
                print(f"✗ {label} not created" if label in failed else f"✓ {label} created")
            code = 1 if failed else 0
        else:
            names = " ".join(shlex.quote(label) for label in missing)
            fix = f"gh label create {names}" if len(missing) == 1 else f'for label in {names}; do gh label create "$label"; done'
            print(f"  → create {'it' if len(missing) == 1 else 'them'} later: {fix}")
            code = 1
    if roles:
        print(f"Commit {name} before the next run: ralph-kit run needs a clean working tree.")
    return code


def main(argv=None, prog=None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path("ralph.toml"), metavar="PATH")
    args = parser.parse_args(argv)  # Exits with status 2 on unknown arguments.
    root = pathlib.Path.cwd().resolve()
    path = args.config if args.config.is_absolute() else root / args.config
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    return run(root, path, confirm=confirm_with_arrows if interactive else None)
