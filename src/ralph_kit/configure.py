"""`ralph-kit configure`: change sections of an existing configuration, then save only what changed."""

from __future__ import annotations

import dataclasses
import pathlib
import shlex
import shutil
from collections.abc import Callable

from .config import (
    BACKUP_MODEL_KEY,
    MODEL_ROLES,
    AgentProfile,
    MissingConfigError,
    RalphConfig,
    RalphConfigError,
    load_config,
)
from .init import (
    CONFIG_NAME,
    CommandRunner,
    InitAborted,
    Prompter,
    current_login,
    option,
    render,
    run_command,
)


SAVE = "Save"
QUIT = "Quit without saving"
BACK = "Back"
NOT_YET = "not editable yet: edit the file directly"
CANCELLED = "configure cancelled, nothing written"


class ConfigureError(RuntimeError):
    """configure cannot open or save this configuration."""


class ConfigureAborted(RuntimeError):
    """The user interrupted configure; nothing was written."""


def _profile_summary(profile: AgentProfile) -> str:
    return f"{profile.engine}/{profile.name} {profile.reasoning_effort}"


def _models_summary(config: RalphConfig) -> str:
    parts = []
    for role in MODEL_ROLES:
        profile = config.models.get(role)
        if profile is None:
            continue
        if role == "correction" and profile == config.models.get("implementation"):
            parts.append("correction = implementation")
        else:
            parts.append(f"{role} {_profile_summary(profile)}")
    return " · ".join(parts)


def _on(value: bool) -> str:
    return "on" if value else "off"


# Each section: its one-line summary, and which changed keys mark it changed. EDITORS lists the editable ones.
SECTIONS: dict[str, tuple[Callable[[RalphConfig], str], Callable[[str], bool]]] = {
    "Model profiles": (_models_summary, lambda key: key.split(".")[:2] in [["models", role] for role in MODEL_ROLES]),
    "Backup": (
        lambda config: _profile_summary(config.backup) if config.backup else "none",
        lambda key: key.startswith(f"models.{BACKUP_MODEL_KEY}."),
    ),
    "Trusted authors": (lambda config: ", ".join(sorted(config.trusted_authors)), lambda key: key == "trusted_authors"),
    "Verification": (
        lambda config: f"local {_on(config.verification.local_checks)} · CI {_on(config.verification.ci_checks)}",
        lambda key: key.startswith("verification."),
    ),
    "Git": (
        lambda config: f"{config.repository} · {config.base_branch} · {config.remote}",
        lambda key: key in {"repository", "base_branch", "remote"},
    ),
}


def _toml_values(config: RalphConfig, root: pathlib.Path) -> dict[str, str]:
    """Every configured value by its TOML key, as the renderer writes it."""
    keys = {
        "repository": config.repository,
        "base_branch": config.base_branch,
        "remote": config.remote,
        "trusted_authors": ", ".join(sorted(config.trusted_authors)),
        **{f"labels.{key}": value for key, value in config.labels.items()},
    }
    for role, profile in {**config.models, BACKUP_MODEL_KEY: config.backup}.items():
        if profile is not None:
            keys |= {f"models.{role}.{field}": getattr(profile, field) for field in ("engine", "name", "display_name", "reasoning_effort")}
    verification = config.verification
    keys |= {
        "verification.local_checks": str(verification.local_checks).lower(),
        "verification.ci_checks": str(verification.ci_checks).lower(),
        "verification.quality_module": verification.quality_module.relative_to(root).as_posix(),
        "verification.quality_manifest": verification.quality_manifest.relative_to(root).as_posix(),
    }
    return keys


def changes(before: RalphConfig, after: RalphConfig, root: pathlib.Path) -> dict[str, tuple[str | None, str | None]]:
    """Each changed key with its old and new value; None when the key is absent."""
    old, new = _toml_values(before, root), _toml_values(after, root)
    return {key: (old.get(key), new.get(key)) for key in dict.fromkeys([*old, *new]) if old.get(key) != new.get(key)}


def _comments(contents: str) -> list[str]:
    """The comments of a TOML file, outside strings."""
    found = []
    for line in contents.splitlines():
        quote = None
        index = 0
        while index < len(line):
            character = line[index]
            if quote:
                if character == "\\" and quote == '"':
                    index += 1
                elif character == quote:
                    quote = None
            elif character in "\"'":
                quote = character
            elif character == "#":
                found.append(line[index:].strip())
                break
            index += 1
    return found


def _author_error(value: str) -> str | None:
    return "a GitHub account name, without spaces" if any(character.isspace() for character in value) else None


def _edit_trusted_authors(prompter: Prompter, config: RalphConfig, command: CommandRunner) -> RalphConfig:
    login = current_login(command)  # None without gh: no warning.
    warned = False
    while True:
        authors = config.trusted_authors
        if not login or login in authors:
            warned = False
        elif not warned:  # Once, then again only if it becomes untrusted after being trusted.
            prompter.say(f"  ⚠ The current gh account, {login}, is not a trusted author: Ralph would ignore its own implementation evidence.")
            warned = True
        answer = prompter.choice(
            f"Trusted authors: {', '.join(sorted(authors))} — what do you want to do?",
            [("Add an author", ""), ("Remove an author", ""), (BACK, "")],
            BACK,
        )
        if answer == BACK:
            return config
        if answer == "Remove an author":
            if len(authors) == 1:
                prompter.say(f"  ✗ {next(iter(authors))} is the last trusted author: add another one before removing it.")
                continue
            keep = "Keep them all"  # Not a GitHub account name: those have no spaces.
            author = prompter.choice("Which author do you want to remove?", [*((name, "") for name in sorted(authors)), (keep, "")], keep)
            if author != keep:
                config = dataclasses.replace(config, trusted_authors=authors - {author})
                prompter.say(f"  ✓ {author} removed")
            continue
        author = prompter.text("GitHub account to trust (leave empty to go back)", validate=_author_error, required=False)
        if not author:
            continue
        if author in authors:
            prompter.say(f"  {author} is already a trusted author")
            continue
        config = dataclasses.replace(config, trusted_authors=authors | {author})
        prompter.say(f"  ✓ {author} added")


def _edit_verification(prompter: Prompter, config: RalphConfig, command: CommandRunner) -> RalphConfig:
    while True:
        verification = config.verification
        answer = prompter.choice(
            "Verification: which check do you want to turn on or off?",
            [
                ("Local checks", f"{_on(verification.local_checks)}, the local gates after each session"),
                ("CI checks", f"{_on(verification.ci_checks)}, the CI checks of the draft PR"),
                (BACK, ""),
            ],
            BACK,
        )
        if answer == BACK:
            return config
        field = "local_checks" if answer == "Local checks" else "ci_checks"
        enabled = not getattr(verification, field)
        config = dataclasses.replace(config, verification=dataclasses.replace(verification, **{field: enabled}))
        prompter.say(f"  {answer} {_on(enabled)}")
        missing = [path for path in (verification.quality_module, verification.quality_manifest) if not path.is_file()]
        if enabled and missing:
            names = " and ".join(path.name for path in missing)
            prompter.say(f"  ⚠ {names} missing: `ralph-kit check` will fail until {'they exist' if len(missing) > 1 else 'it exists'}.")


EDITORS: dict[str, Callable[[Prompter, RalphConfig, CommandRunner], RalphConfig]] = {
    "Trusted authors": _edit_trusted_authors,
    "Verification": _edit_verification,
}


def _menu(prompter: Prompter, title: str, config: RalphConfig, changed: dict[str, tuple[str | None, str | None]]) -> str:
    choices = []
    for name, (summary, owns) in SECTIONS.items():
        marker = "* " if any(owns(key) for key in changed) else "  "
        choices.append(option(f"{marker}{name}", summary(config), None if name in EDITORS else NOT_YET))
    choices += [(f"  {SAVE}", ""), (f"  {QUIT}", "")]
    return prompter.choice(f"{title} — what do you want to change?", choices, f"  {SAVE}").strip().removeprefix("* ")


def _shown(value: str | None) -> str:
    return "(none)" if value is None else value


def run(
    root: pathlib.Path,
    prompter: Prompter,
    *,
    config: pathlib.Path = pathlib.Path(CONFIG_NAME),
    command: CommandRunner | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> bool:
    """Edit the configuration at config; True when it was written.

    command and which supply detection hints inside a section; nothing detected overwrites a value.
    """
    command = command or (lambda *args: run_command(*args, cwd=root))
    target = root / config  # An absolute config replaces root.
    display_path = target.relative_to(root) if target.is_relative_to(root) else target
    option_text = "" if target == root / CONFIG_NAME else f" --config {shlex.quote(str(display_path))}"
    if not prompter.interactive:
        raise ConfigureError(f"ralph-kit configure needs an interactive terminal; edit {display_path} directly")
    try:
        original = load_config(target, root, require_review=False)
    except MissingConfigError as error:
        raise ConfigureError(str(error)) from None
    except RalphConfigError as error:
        raise ConfigureError(
            f"{error}\n   Fix {display_path}, or start over with `ralph-kit init{option_text} --force`."
        ) from None

    rendered = render(original, root)
    if set(_comments(target.read_text(encoding="utf-8"))) - set(_comments(rendered)):
        prompter.say(f"ℹ {display_path} has comments that ralph-kit does not write: saving will drop them.")

    edited = original
    try:
        while True:
            answer = _menu(prompter, str(display_path), edited, changes(original, edited, root))
            if answer == QUIT:
                prompter.say("Nothing written.")
                return False
            if answer == SAVE:
                break
            edited = EDITORS[answer](prompter, edited, command)
    except InitAborted:
        raise ConfigureAborted(CANCELLED) from None

    changed = changes(original, edited, root)
    if not changed:
        prompter.say("No changes: nothing written.")
        return False
    prompter.say("\nChanges:")
    for key, (old, new) in changed.items():
        prompter.say(f"  {key}: {_shown(old)} → {_shown(new)}")
    # Validated next to the target before replacing it, so a renderer bug never costs the configuration.
    staged = target.with_name(f".{target.name}.configure")
    staged.write_text(render(edited, root), encoding="utf-8")
    try:
        load_config(staged, root, require_review="review" in edited.models)
    except RalphConfigError as error:  # The renderer must always produce a loadable file.
        staged.unlink()
        raise ConfigureError(f"saved configuration is invalid, please report this bug: {error}") from error
    staged.replace(target)
    prompter.say(f"\n✅ Wrote {target}.")
    prompter.say(f"   Next step: commit {display_path}, then run `ralph-kit check{option_text}`.")
    return True
