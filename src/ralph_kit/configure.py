"""`ralph-kit configure`: change sections of an existing configuration, then save only what changed."""

from __future__ import annotations

import dataclasses
import pathlib
import shlex
import shutil
from collections.abc import Callable
from typing import NamedTuple

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
    ENGINES,
    CommandRunner,
    InitAborted,
    Prompter,
    ask_profile,
    backup_engines,
    codex_catalog,
    create_offered_labels,
    current_login,
    detect_repository,
    git_name_error,
    git_remotes,
    offer_labels,
    remote_default_branch,
    render,
    repository_error,
    run_command,
)


SAVE = "Save"
QUIT = "Quit without saving"
BACK = "Back"
CANCELLED = "configure cancelled, nothing written"


class ConfigureError(RuntimeError):
    """configure cannot open or save this configuration."""


class ConfigureAborted(RuntimeError):
    """The user interrupted configure; nothing was written."""


@dataclasses.dataclass
class EditState:
    """What the sections share besides the edited configuration."""

    command: CommandRunner
    saved_repository: str  # The repository in the file: going back to it is no repository change.
    installed: list[str]  # Agent CLIs found on this machine.
    # Missing labels ({role: name}) the user agreed to create on the edited repository once the file is written.
    labels_to_create: dict[str, str] = dataclasses.field(default_factory=dict)
    catalogs: dict[str, list[dict]] = dataclasses.field(default_factory=dict)

    def catalog(self, engine: str) -> list[dict]:
        """The engine's model catalog, asked once; [] for Claude or when Codex cannot list it."""
        if engine not in self.catalogs:
            self.catalogs[engine] = codex_catalog(self.command) if engine == "codex" else []
        return self.catalogs[engine]


def _profile_summary(profile: AgentProfile) -> str:
    return f"{profile.engine}/{profile.name} {profile.reasoning_effort}"


def _role_summary(config: RalphConfig, role: str) -> str:
    profile = config.models.get(role)
    if profile is None:
        return "none"
    if role == "correction" and profile == config.models.get("implementation"):
        return "same as implementation"
    return _profile_summary(profile)


def _models_summary(config: RalphConfig) -> str:
    return " · ".join(
        "correction = implementation" if summary == "same as implementation" else f"{role} {summary}"
        for role in MODEL_ROLES
        if (summary := _role_summary(config, role)) != "none"
    )


def _on(value: bool) -> str:
    return "on" if value else "off"


# Each section: its one-line summary, and which changed keys mark it changed; EDITORS edits each one.
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
    "Reviewed runs by default": (lambda config: _on(config.review_by_default), lambda key: key == "review_by_default"),
    "Keep the machine awake by default": (
        lambda config: _on(config.caffeinate_by_default),
        lambda key: key == "caffeinate_by_default",
    ),
}


def _toml_values(config: RalphConfig, root: pathlib.Path) -> dict[str, str]:
    """Every configured value by its TOML key, as the renderer writes it."""
    keys = {
        "repository": config.repository,
        "base_branch": config.base_branch,
        "remote": config.remote,
        "trusted_authors": ", ".join(sorted(config.trusted_authors)),
        "review_by_default": str(config.review_by_default).lower(),
        "caffeinate_by_default": str(config.caffeinate_by_default).lower(),
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


def _edit_trusted_authors(prompter: Prompter, config: RalphConfig, state: EditState) -> RalphConfig:
    with prompter.working("Reading the current gh account…"):
        login = current_login(state.command)  # None without gh: no warning.
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


def _choose_engine(prompter: Prompter, role: str, current: str, installed: list[str]) -> str:
    """The role's engine among the installed CLIs and its current one, which may not be installed here."""
    engines = [engine for engine in ENGINES if engine in installed or engine == current]
    if len(engines) == 1:
        prompter.say(f"  Engine: {current} ({ENGINES[current]}, no other agent CLI installed)")
        return current
    return prompter.choice(
        f"Engine for {role}",
        [(engine, ENGINES[engine] + ("" if engine in installed else ", not installed here")) for engine in engines],
        current,
    )


def _ask_model(
    prompter: Prompter,
    state: EditState,
    engine: str,
    current: AgentProfile | None,
    role: str,
    question: str,
    effort: str,
    avoid: dict[str, AgentProfile],
) -> AgentProfile:
    """A model profile on engine that differs from every profile in avoid; effort is the default without current.

    On current's engine, current's model and effort are the defaults, and keeping its model keeps its display name.
    """
    same_engine = current is not None and current.engine == engine
    profile = AgentProfile(**ask_profile(
        prompter, engine, state.catalog(engine), role, question,
        effort=current.reasoning_effort if current else effort,
        avoid={key: dataclasses.asdict(other) for key, other in avoid.items()},
        current=current.name if same_engine else None,
    ))
    if same_engine and profile.name == current.name:  # The same model keeps the name it is shown with.
        profile = dataclasses.replace(profile, display_name=current.display_name)
    return profile


def _ask_role_profile(prompter: Prompter, config: RalphConfig, state: EditState, role: str) -> AgentProfile:
    """The role's model profile, with its current one as the default.

    A configuration without review starts it from implementation's engine and a high effort.
    """
    current = config.models.get(role)
    engine = _choose_engine(prompter, role, (current or config.models["implementation"]).engine, state.installed)
    return _ask_model(
        prompter, state, engine, current, role, f"Model for {role}",
        effort="high" if role == "review" else "medium",
        avoid={BACKUP_MODEL_KEY: config.backup} if config.backup else {},
    )


def _edit_model_profiles(prompter: Prompter, config: RalphConfig, state: EditState) -> RalphConfig:
    while True:
        role = prompter.choice(
            "Model profiles: which session role do you want to change?",
            [*((role, _role_summary(config, role)) for role in MODEL_ROLES), (BACK, "")],
            BACK,
        )
        if role == BACK:
            return config
        current = config.models.get(role)
        profile = _ask_role_profile(prompter, config, state, role)
        if profile == current:
            continue
        models = {**config.models, role: profile}
        if role == "implementation" and config.models.get("correction") == current:
            follows = prompter.choice(
                "Correction has the same model profile as implementation: should it follow?",
                [("Yes", f"correction becomes {_profile_summary(profile)}"), ("No", f"correction keeps {_profile_summary(current)}")],
                "Yes",
            )
            if follows == "Yes":
                models["correction"] = profile
        config = dataclasses.replace(config, models=models)
        prompter.say(f"  ✓ {role}: {_profile_summary(profile)}")


def _ask_backup(prompter: Prompter, config: RalphConfig, state: EditState) -> AgentProfile:
    """A backup different from every session role's model profile, from the current one when there is one."""
    current = config.backup
    options = backup_engines(
        {profile.engine for profile in config.models.values()}, state.installed, current.engine if current else None
    )
    if len(options) == 1:
        engine = options[0][0]
        prompter.say(f"  Engine: {engine} ({ENGINES[engine]}, no other agent CLI installed)")
    else:
        engine = prompter.choice("Engine for the backup", options, current.engine if current else options[0][0])
    return _ask_model(prompter, state, engine, current, BACKUP_MODEL_KEY, "Model for the backup", "medium", config.models)


def _edit_backup(prompter: Prompter, config: RalphConfig, state: EditState) -> RalphConfig:
    while True:
        backup = config.backup
        actions = [("Change the backup", ""), ("Remove the backup", "")] if backup else [("Add a backup", "")]
        answer = prompter.choice(
            f"Backup: {_profile_summary(backup) if backup else 'none'} — what do you want to do?",
            [*actions, (BACK, "")],
            BACK,
        )
        if answer == BACK:
            return config
        if answer == "Remove the backup":
            config = dataclasses.replace(config, backup=None)
            prompter.say("  ✓ backup removed")
            continue
        profile = _ask_backup(prompter, config, state)
        if profile != backup:
            config = dataclasses.replace(config, backup=profile)
            prompter.say(f"  ✓ backup: {_profile_summary(profile)}")


def _edit_verification(prompter: Prompter, config: RalphConfig, state: EditState) -> RalphConfig:
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


def _edit_review_by_default(prompter: Prompter, config: RalphConfig, state: EditState) -> RalphConfig:
    enabled = config.review_by_default
    answer = prompter.choice(
        f"Reviewed runs by default: {_on(enabled)} — what do you want to do?",
        [
            ("Turn off", "runs are standard unless started with --review") if enabled
            else ("Turn on", "every run is a reviewed run unless started with --no-review"),
            (BACK, ""),
        ],
        BACK,
    )
    if answer == BACK:
        return config
    if not enabled and "review" not in config.models:
        prompter.say("  Reviewed runs need a model profile for review.")
        profile = _ask_role_profile(prompter, config, state, "review")
        config = dataclasses.replace(config, models={**config.models, "review": profile})
        prompter.say(f"  ✓ review: {_profile_summary(profile)}")
    prompter.say(f"  Reviewed runs by default {_on(not enabled)}")  # Turning it off keeps the review profile.
    return dataclasses.replace(config, review_by_default=not enabled)


def _edit_caffeinate_by_default(prompter: Prompter, config: RalphConfig, state: EditState) -> RalphConfig:
    enabled = config.caffeinate_by_default
    answer = prompter.choice(
        f"Keep the machine awake by default: {_on(enabled)} — what do you want to do?",
        [
            ("Turn off", "runs hold no sleep lock unless started with --caffeinate") if enabled
            else ("Turn on", "every run holds a sleep lock unless started with --no-caffeinate"),
            (BACK, ""),
        ],
        BACK,
    )
    if answer == BACK:
        return config
    prompter.say(f"  Keep the machine awake by default {_on(not enabled)}")
    return dataclasses.replace(config, caffeinate_by_default=not enabled)


class GitField(NamedTuple):
    key: str
    question: str
    error: Callable[[str], str | None]
    hint: str | None  # What detection found, when it differs from the current value.


def _detected_hint(current: str, detected: str | None) -> str | None:
    return f"detected {detected}" if detected and detected != current else None


def _with_hint(text: str, hint: str | None) -> str:
    return f"{text}; {hint}" if hint else text


def _edit_git(prompter: Prompter, config: RalphConfig, state: EditState) -> RalphConfig:
    command = state.command
    with prompter.working("Detecting the GitHub repository…"):
        detected_repository, detected_branch = detect_repository(command)
        remotes = git_remotes(command)
    while True:
        # gh's default branch belongs to the working directory's repository, which the configuration may not name.
        if detected_branch and detected_repository == config.repository:
            branch = detected_branch
        else:
            branch = remote_default_branch(command, config.remote)
        fields = {
            "Repository": GitField(
                "repository", "GitHub repository (owner/name)", repository_error,
                _detected_hint(config.repository, detected_repository),
            ),
            "Base branch": GitField(
                "base_branch", "Base branch (Ralph never commits to it)", git_name_error,
                _detected_hint(config.base_branch, branch),
            ),
            "Remote": GitField(
                "remote", "Remote Ralph pushes to", git_name_error,
                f"Git remotes: {', '.join(remotes)}" if remotes and remotes != [config.remote] else None,
            ),
        }
        answer = prompter.choice(
            "Git: which value do you want to change?",
            [*((name, _with_hint(getattr(config, field.key), field.hint)) for name, field in fields.items()), (BACK, "")],
            BACK,
        )
        if answer == BACK:
            return config
        field = fields[answer]
        current = getattr(config, field.key)
        value = prompter.text(_with_hint(field.question, field.hint), default=current, validate=field.error)
        if value == current:
            continue
        config = dataclasses.replace(config, **{field.key: value})
        if field.key == "repository":  # Replaces any offer made for a repository chosen before.
            changed = value != state.saved_repository
            state.labels_to_create = offer_labels(prompter, command, value, config.labels) if changed else {}


EDITORS: dict[str, Callable[[Prompter, RalphConfig, EditState], RalphConfig]] = {
    "Model profiles": _edit_model_profiles,
    "Backup": _edit_backup,
    "Trusted authors": _edit_trusted_authors,
    "Verification": _edit_verification,
    "Git": _edit_git,
    "Reviewed runs by default": _edit_review_by_default,
    "Keep the machine awake by default": _edit_caffeinate_by_default,
}


def _menu(prompter: Prompter, title: str, config: RalphConfig, changed: dict[str, tuple[str | None, str | None]]) -> str:
    choices = []
    for name, (summary, owns) in SECTIONS.items():
        marker = "* " if any(owns(key) for key in changed) else "  "
        choices.append((f"{marker}{name}", summary(config)))
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
    state = EditState(command, original.repository, [engine for engine in ENGINES if which(engine)])
    try:
        while True:
            answer = _menu(prompter, str(display_path), edited, changes(original, edited, root))
            if answer == QUIT:
                prompter.say("Nothing written.")
                return False
            if answer == SAVE:
                break
            edited = EDITORS[answer](prompter, edited, state)
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
        load_config(staged, root, require_review="review" in edited.models or edited.review_by_default)
    except RalphConfigError as error:  # The renderer must always produce a loadable file.
        staged.unlink()
        raise ConfigureError(f"saved configuration is invalid, please report this bug: {error}") from error
    staged.replace(target)
    prompter.say(f"\n✅ Wrote {target}.")
    if state.labels_to_create:
        create_offered_labels(prompter, command, edited.repository, state.labels_to_create)
    prompter.say(f"   Next step: commit {display_path}, then run `ralph-kit check{option_text}`.")
    return True
