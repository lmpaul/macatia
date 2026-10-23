"""Write the defaulted settings the configuration leaves out, then offer the missing labels on GitHub.

The configuration is edited in place, one line per added setting and its comment, so its layout and comments stay.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import re
import shlex
import sys
import tomllib

from .check import Readiness, confirm_with_arrows, create_labels, missing_labels
from .config import (
    DEFAULT_LABELS,
    RalphConfig,
    RalphConfigError,
    _load_toml,
    load_config,
)
from .init import TOP_LEVEL_COMMENTS, top_level_setting


TABLE_HEADER = re.compile(r"\s*\[")
COMMENT = re.compile(r"\s*#")
DEFAULTS = {field.name: field.default for field in dataclasses.fields(RalphConfig)}
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


def inserted(lines: list[str], end: int, added: list[str]) -> str:
    """The text of lines with added before lines[end], in the file's line endings."""
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    if end == len(lines) and lines and not lines[-1].endswith("\n"):
        lines = [*lines[:-1], lines[-1] + newline]  # Only a line followed by added ones gains a line break.
    return "".join(lines[:end] + [line + newline for line in added] + lines[end:])


def with_top_level(text: str, keys: list[str]) -> str:
    """text with each key, under its comment, after the last top-level setting, before the first table.

    A comment block directly above the first table belongs to that table and stays with it.
    """
    lines = text.splitlines(keepends=True)
    end = next((index for index, line in enumerate(lines) if TABLE_HEADER.match(line)), len(lines))
    if end < len(lines):
        while end > 0 and COMMENT.match(lines[end - 1]):
            end -= 1
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    return inserted(lines, end, [line for key in keys for line in top_level_setting(key, DEFAULTS[key])])


def with_roles(text: str, roles: dict[str, str]) -> str:
    """text with one line per role at the end of its [labels] table, after the last non-blank line."""
    lines = text.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if LABELS_HEADER.fullmatch(line.rstrip("\r\n"))), None)
    if start is None:
        raise UpdateError("no [labels] table to complete: add the missing roles by hand")
    end = next((index for index in range(start + 1, len(lines)) if TABLE_HEADER.match(lines[index])), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return inserted(lines, end, [f"{role} = {json.dumps(name, ensure_ascii=False)}" for role, name in roles.items()])


def completed_text(path: pathlib.Path) -> tuple[str, dict[str, str]]:
    """The file's text with the defaulted settings added, and each added key with its value; empty when there is none."""
    values = _load_toml(path)
    labels = values.get("labels")
    if not isinstance(labels, dict):
        raise UpdateError("no [labels] table to complete: add the missing roles by hand")
    keys = [key for key in TOP_LEVEL_COMMENTS if key not in values]
    roles = missing_roles(labels)
    text = path.read_bytes().decode("utf-8")  # Not read_text, which would turn CRLF line endings into LF.
    if keys:
        text = with_top_level(text, keys)
    if roles:
        text = with_roles(text, roles)
    if not keys and not roles:
        return text, {}
    try:
        completed = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise UpdateError(f"cannot add {', '.join([*keys, *roles])} to this file's layout: add them by hand") from None
    if not set(DEFAULT_LABELS) <= set(completed.get("labels", {})):
        raise UpdateError("no [labels] table to complete: add the missing roles by hand")
    if any(completed.get(key) != DEFAULTS[key] for key in keys):
        raise UpdateError(f"cannot add {', '.join(keys)} before the first table: add {'it' if len(keys) == 1 else 'them'} by hand")
    added = {key: str(DEFAULTS[key]).lower() for key in keys}
    return text, added | {role: json.dumps(name, ensure_ascii=False) for role, name in roles.items()}


def load_completed(path: pathlib.Path, root: pathlib.Path) -> tuple[RalphConfig, dict[str, str]]:
    """Complete the file, writing it only once the completed text loads; return its configuration and the added settings."""
    text, added = completed_text(path)
    if not added:
        return load_config(path, root, require_review=False), added
    # Validated next to the target before replacing it, so a file still invalid once completed is left as it was.
    staged = path.with_name(f".{path.name}.update-config")
    try:
        staged.write_bytes(text.encode("utf-8"))
        config = load_config(staged, root, require_review=False)
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)
    return config, added


def run(root: pathlib.Path, config_path: pathlib.Path, *, confirm=None, command=None) -> int:
    """confirm(question) -> bool offers to create missing labels; None never writes to GitHub.

    command(*args) -> stdout runs gh; it raises RuntimeError on failure.
    """
    command = command or Readiness(root).command
    name = config_path.name
    try:
        config, added = load_completed(config_path, root)
    except (UpdateError, RalphConfigError, tomllib.TOMLDecodeError) as error:
        print(f"❌ {error}", file=sys.stderr)
        return 1
    for key, value in added.items():
        print(f"✓ {name}: added {key} = {value}")
    if not added:
        print(f"✓ {name}: nothing to add, every defaulted setting is declared")

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
    if added:
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
