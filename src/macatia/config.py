"""Load and validate the repository-specific Macatia configuration."""

from __future__ import annotations

import pathlib
import re
import tomllib
from dataclasses import dataclass
from typing import Any


# Each label role with the name init gives it, in the order init writes them.
DEFAULT_LABELS = {
    "needs_triage": "needs-triage",
    "needs_info": "needs-info",
    "ready_for_agent": "ready-for-agent",
    "ready_for_human": "ready-for-human",
    "wontfix": "wontfix",
    "agent_working": "agent-working",
    "human_review": "human-review",
    "implemented": "implemented",
    "work_started": "work-started",
    "standalone": "standalone",
}
LABEL_KEYS = set(DEFAULT_LABELS)
# Label roles added after the first release: a file written before one keeps loading with
# the default name, which macatia check then offers to create on GitHub.
LATER_LABEL_DEFAULTS = {key: DEFAULT_LABELS[key] for key in ("work_started", "standalone")}
# Suggested for unknown keys, which usually come from a newer macatia: the version only
# changes on release, so a plain upgrade can keep an older build installed from Git.
UPGRADE_COMMAND = "uv tool upgrade macatia --reinstall"
MODEL_ROLES = ("implementation", "review", "correction")
MODEL_KEYS = set(MODEL_ROLES)
# The optional profile that takes over every session role after an engine failure.
BACKUP_MODEL_KEY = "backup"
# Each child session limit key with its default: a child's worst case under the correction
# limit, plus one.
DEFAULT_CHILD_SESSION_LIMITS = {"standard": 5, "reviewed": 9}
# A reviewed child always needs room for its review session.
MINIMUM_CHILD_SESSION_LIMITS = {"standard": 1, "reviewed": 2}
REASONING_EFFORTS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
}
ENGINES = {"codex", "claude"}
CLAUDE_REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
# Run diagnostics live in the repository and must stay ignored by Git.
DIAGNOSTICS_DIRECTORY = ".macatia"
# A run started on the base branch offers the run branch named this prefix plus the parent.
RUN_BRANCH_PREFIX = "macatia/issue-"


class MacatiaConfigError(RuntimeError):
    """The TOML exists but cannot safely configure a Macatia run."""


class MissingConfigError(MacatiaConfigError):
    """No configuration file yet: the repository has not been set up with macatia init."""


@dataclass(frozen=True)
class AgentProfile:
    name: str
    display_name: str
    reasoning_effort: str
    engine: str = "codex"


@dataclass(frozen=True)
class VerificationConfig:
    local_checks: bool
    ci_checks: bool
    quality_module: pathlib.Path
    quality_manifest: pathlib.Path

    @property
    def needs_quality_policy(self) -> bool:
        return self.ci_checks or self.local_checks

    @property
    def has_automated_verification(self) -> bool:
        return self.ci_checks or self.local_checks


@dataclass(frozen=True)
class ChildSessionLimits:
    """The maximum number of agent sessions one child may receive in a run of each mode."""

    standard: int = DEFAULT_CHILD_SESSION_LIMITS["standard"]
    reviewed: int = DEFAULT_CHILD_SESSION_LIMITS["reviewed"]

    def for_run(self, *, review: bool) -> int:
        return self.reviewed if review else self.standard


@dataclass(frozen=True)
class MacatiaConfig:
    repository: str
    base_branch: str
    remote: str
    trusted_authors: frozenset[str]
    labels: dict[str, str]
    models: dict[str, AgentProfile]
    verification: VerificationConfig
    # Not one of `models`, which holds a profile per session role.
    backup: AgentProfile | None = None
    # Makes a run without --review or --no-review a reviewed run.
    review_by_default: bool = False
    # Makes a run without --caffeinate or --no-caffeinate hold the sleep lock.
    caffeinate_by_default: bool = False
    child_session_limits: ChildSessionLimits = ChildSessionLimits()
    # The shell command that prepares a run worktree; None when the file leaves it out.
    worktree_setup: str | None = None
    # Settings the file leaves out, which got their default value, as dotted keys such as
    # child_session_limit.reviewed or labels.work_started. Never models.review, models.backup or worktree_setup:
    # leaving them out turns the feature off.
    defaulted_settings: frozenset[str] = frozenset()

    @property
    def defaulted_labels(self) -> frozenset[str]:
        """Label roles the file leaves out, which got their default name."""
        return frozenset(
            setting.removeprefix("labels.")
            for setting in self.defaulted_settings
            if setting.startswith("labels.")
        )

    @property
    def triage_labels(self) -> frozenset[str]:
        return frozenset(
            self.labels[key]
            for key in (
                "needs_triage",
                "needs_info",
                "ready_for_agent",
                "ready_for_human",
                "wontfix",
            )
        )

    @property
    def lifecycle_labels(self) -> frozenset[str]:
        return frozenset(
            self.labels[key]
            for key in ("ready_for_agent", "agent_working", "human_review", "implemented")
        )


# The configuration file's name before the project was renamed to Macatia.
LEGACY_CONFIG_NAME = "ralph.toml"
LEGACY_CONFIG_RENAME = f"git mv {LEGACY_CONFIG_NAME} macatia.toml"


def legacy_config(path: pathlib.Path) -> bool:
    """Whether a missing macatia.toml has a pre-rename ralph.toml beside it."""
    return path.name == "macatia.toml" and path.with_name(LEGACY_CONFIG_NAME).is_file()


def _load_toml(path: pathlib.Path) -> Any:
    try:
        with path.open("rb") as configuration_file:
            return tomllib.load(configuration_file)
    except FileNotFoundError:
        shown = path.relative_to(pathlib.Path.cwd()) if path.is_relative_to(pathlib.Path.cwd()) else path
        if legacy_config(path):
            raise MissingConfigError(
                f"{shown} not found, but {LEGACY_CONFIG_NAME} is: rename it with `{LEGACY_CONFIG_RENAME}`"
            ) from None
        raise MissingConfigError(f"{shown} not found: run `macatia init` first to set up this repository") from None
    except OSError as error:
        raise MacatiaConfigError(f"could not read Macatia configuration {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise MacatiaConfigError(
            f"invalid TOML in Macatia configuration {path}: {error}"
        ) from error


def _mapping(
    value: Any, path: str, expected_keys: set[str], optional_keys: set[str] | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MacatiaConfigError(f"{path} must be a mapping")
    actual_keys = set(value)
    if unknown := actual_keys - expected_keys - (optional_keys or set()):
        raise MacatiaConfigError(
            f"{path} has unknown keys: {', '.join(sorted(unknown))}"
            f" (if a newer macatia wrote them, upgrade with `{UPGRADE_COMMAND}`)"
        )
    if missing := expected_keys - actual_keys:
        raise MacatiaConfigError(f"{path} is missing keys: {', '.join(sorted(missing))}")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MacatiaConfigError(f"{path} must be a non-empty string")
    if value != value.strip():
        raise MacatiaConfigError(f"{path} must not have surrounding whitespace")
    return value


def _relative_path(value: Any, path: str, repository_root: pathlib.Path) -> pathlib.Path:
    configured = pathlib.Path(_string(value, path))
    if configured.is_absolute() or ".." in configured.parts:
        raise MacatiaConfigError(f"{path} must stay inside the repository")
    return repository_root / configured


def _profile(value: Any, path: str) -> AgentProfile:
    values = _mapping(value, path, {"name", "display_name", "reasoning_effort"}, {"engine"})
    engine = _string(values.get("engine", "codex"), f"{path}.engine")
    if engine not in ENGINES:
        raise MacatiaConfigError(f"{path}.engine must be codex or claude")
    effort = _string(values["reasoning_effort"], f"{path}.reasoning_effort")
    supported_efforts = CLAUDE_REASONING_EFFORTS if engine == "claude" else REASONING_EFFORTS
    if effort not in supported_efforts:
        raise MacatiaConfigError(
            f"{path}.reasoning_effort must be one of: "
            + ", ".join(sorted(supported_efforts))
        )
    return AgentProfile(
        name=_string(values["name"], f"{path}.name"),
        display_name=_string(values["display_name"], f"{path}.display_name"),
        reasoning_effort=effort,
        engine=engine,
    )


def child_session_limit_error(key: str, limit: Any) -> str | None:
    """Why limit cannot be the child session limit for key, or None when it can."""
    # TOML booleans load as bool, a subclass of int.
    if not isinstance(limit, int) or isinstance(limit, bool):
        return "must be an integer"
    if limit < MINIMUM_CHILD_SESSION_LIMITS[key]:
        return f"must be at least {MINIMUM_CHILD_SESSION_LIMITS[key]}"
    return None


def _child_session_limits(value: Any, path: str) -> ChildSessionLimits:
    values = _mapping(value, path, set(), set(DEFAULT_CHILD_SESSION_LIMITS))
    limits = {}
    for key in MINIMUM_CHILD_SESSION_LIMITS:
        limit = values.get(key, DEFAULT_CHILD_SESSION_LIMITS[key])
        if error := child_session_limit_error(key, limit):
            raise MacatiaConfigError(f"{path}.{key} {error}")
        limits[key] = limit
    return ChildSessionLimits(**limits)


def load_config(
    path: pathlib.Path, repository_root: pathlib.Path, *, require_review: bool = True
) -> MacatiaConfig:
    """Return one fully validated configuration or raise MacatiaConfigError."""

    values = _mapping(
        _load_toml(path),
        "configuration",
        {
            "version",
            "repository",
            "base_branch",
            "remote",
            "trusted_authors",
            "labels",
            "models",
            "verification",
        },
        {"review_by_default", "caffeinate_by_default", "child_session_limit", "worktree_setup"},
    )
    if values["version"] != 1:
        raise MacatiaConfigError("configuration.version must be 1")

    repository = _string(values["repository"], "configuration.repository")
    if re.fullmatch(r"[^/\s]+/[^/\s]+", repository) is None:
        raise MacatiaConfigError("configuration.repository must use owner/name format")

    base_branch = _string(values["base_branch"], "configuration.base_branch")
    remote = _string(values["remote"], "configuration.remote")
    for name, value in (("base_branch", base_branch), ("remote", remote)):
        if value.startswith("-") or any(character.isspace() for character in value):
            raise MacatiaConfigError(f"configuration.{name} is not a safe Git name")

    authors = values["trusted_authors"]
    if not isinstance(authors, list) or not authors:
        raise MacatiaConfigError("configuration.trusted_authors must be a non-empty list")
    trusted_authors = frozenset(
        _string(author, f"configuration.trusted_authors[{index}]")
        for index, author in enumerate(authors)
    )
    if len(trusted_authors) != len(authors):
        raise MacatiaConfigError("configuration.trusted_authors must not contain duplicates")

    review_by_default = values.get("review_by_default", False)
    if not isinstance(review_by_default, bool):
        raise MacatiaConfigError("configuration.review_by_default must be a boolean")
    caffeinate_by_default = values.get("caffeinate_by_default", False)
    if not isinstance(caffeinate_by_default, bool):
        raise MacatiaConfigError("configuration.caffeinate_by_default must be a boolean")

    worktree_setup = values.get("worktree_setup")
    if worktree_setup is not None:
        worktree_setup = _string(worktree_setup, "configuration.worktree_setup")

    child_session_limits = _child_session_limits(
        values.get("child_session_limit", {}), "configuration.child_session_limit"
    )

    if isinstance(values["labels"], dict) and (
        missing := sorted(LABEL_KEYS - set(LATER_LABEL_DEFAULTS) - set(values["labels"]))
    ):
        lines = ", ".join(f'{key} = "{DEFAULT_LABELS[key]}"' for key in missing)
        raise MacatiaConfigError(
            f"configuration.labels is missing keys: {', '.join(missing)}"
            f" (add {lines} under [labels], or run macatia update-config)"
        )
    label_values = _mapping(
        values["labels"],
        "configuration.labels",
        LABEL_KEYS - set(LATER_LABEL_DEFAULTS),
        set(LATER_LABEL_DEFAULTS),
    )
    child_session_limit_values = values.get("child_session_limit", {})
    defaulted_settings = frozenset(
        {"review_by_default", "caffeinate_by_default"} - set(values)
        | {
            f"child_session_limit.{key}"
            for key in DEFAULT_CHILD_SESSION_LIMITS
            if key not in child_session_limit_values
        }
        | {f"labels.{key}" for key in LATER_LABEL_DEFAULTS if key not in label_values}
    )
    label_values = LATER_LABEL_DEFAULTS | label_values
    labels = {
        key: _string(label_values[key], f"configuration.labels.{key}")
        for key in LABEL_KEYS
    }
    if len(set(labels.values())) != len(labels):
        raise MacatiaConfigError("configuration.labels values must be unique")

    required_models = MODEL_KEYS if require_review else MODEL_KEYS - {"review"}
    model_values = _mapping(
        values["models"],
        "configuration.models",
        required_models,
        (MODEL_KEYS - required_models) | {BACKUP_MODEL_KEY},
    )
    models = {
        key: _profile(model_values[key], f"configuration.models.{key}")
        for key in model_values
        if key != BACKUP_MODEL_KEY
    }
    backup = None
    if BACKUP_MODEL_KEY in model_values:
        backup = _profile(
            model_values[BACKUP_MODEL_KEY], f"configuration.models.{BACKUP_MODEL_KEY}"
        )
        for key in MODEL_ROLES:
            profile = models.get(key)
            if profile and (profile.engine, profile.name, profile.reasoning_effort) == (
                backup.engine,
                backup.name,
                backup.reasoning_effort,
            ):
                raise MacatiaConfigError(
                    f"configuration.models.{BACKUP_MODEL_KEY} must differ from "
                    f"configuration.models.{key} in engine, model, or reasoning effort"
                )

    verification_values = _mapping(
        values["verification"],
        "configuration.verification",
        {"local_checks", "ci_checks", "quality_module", "quality_manifest"},
    )
    for key in ("local_checks", "ci_checks"):
        if not isinstance(verification_values[key], bool):
            raise MacatiaConfigError(f"configuration.verification.{key} must be a boolean")

    return MacatiaConfig(
        repository=repository,
        base_branch=base_branch,
        remote=remote,
        trusted_authors=trusted_authors,
        labels=labels,
        models=models,
        backup=backup,
        review_by_default=review_by_default,
        caffeinate_by_default=caffeinate_by_default,
        child_session_limits=child_session_limits,
        worktree_setup=worktree_setup,
        defaulted_settings=defaulted_settings,
        verification=VerificationConfig(
            local_checks=verification_values["local_checks"],
            ci_checks=verification_values["ci_checks"],
            quality_module=_relative_path(
                verification_values["quality_module"],
                "configuration.verification.quality_module",
                repository_root,
            ),
            quality_manifest=_relative_path(
                verification_values["quality_manifest"],
                "configuration.verification.quality_manifest",
                repository_root,
            ),
        ),
    )
