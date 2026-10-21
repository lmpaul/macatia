"""Load and validate the repository-specific Ralph configuration."""

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
}
LABEL_KEYS = set(DEFAULT_LABELS)
# Label roles added after the first release: a file written before one keeps loading with
# the default name, which ralph-kit check then offers to create on GitHub.
LATER_LABEL_DEFAULTS = {key: DEFAULT_LABELS[key] for key in ("work_started",)}
# Suggested for unknown keys, which usually come from a newer ralph-kit: the version only
# changes on release, so a plain upgrade can keep an older build installed from Git.
UPGRADE_COMMAND = "uv tool upgrade ralph-kit --reinstall"
MODEL_ROLES = ("implementation", "review", "correction")
MODEL_KEYS = set(MODEL_ROLES)
# The optional profile that takes over every session role after an engine failure.
BACKUP_MODEL_KEY = "backup"
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
DIAGNOSTICS_DIRECTORY = ".ralph"


class RalphConfigError(RuntimeError):
    """The TOML exists but cannot safely configure a Ralph run."""


class MissingConfigError(RalphConfigError):
    """No configuration file yet: the repository has not been set up with ralph-kit init."""


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
class RalphConfig:
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


def _load_toml(path: pathlib.Path) -> Any:
    try:
        with path.open("rb") as configuration_file:
            return tomllib.load(configuration_file)
    except FileNotFoundError:
        shown = path.relative_to(pathlib.Path.cwd()) if path.is_relative_to(pathlib.Path.cwd()) else path
        raise MissingConfigError(f"{shown} not found: run `ralph-kit init` first to set up this repository") from None
    except OSError as error:
        raise RalphConfigError(f"could not read Ralph configuration {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise RalphConfigError(
            f"invalid TOML in Ralph configuration {path}: {error}"
        ) from error


def _mapping(
    value: Any, path: str, expected_keys: set[str], optional_keys: set[str] | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RalphConfigError(f"{path} must be a mapping")
    actual_keys = set(value)
    if unknown := actual_keys - expected_keys - (optional_keys or set()):
        raise RalphConfigError(
            f"{path} has unknown keys: {', '.join(sorted(unknown))}"
            f" (if a newer ralph-kit wrote them, upgrade with `{UPGRADE_COMMAND}`)"
        )
    if missing := expected_keys - actual_keys:
        raise RalphConfigError(f"{path} is missing keys: {', '.join(sorted(missing))}")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RalphConfigError(f"{path} must be a non-empty string")
    if value != value.strip():
        raise RalphConfigError(f"{path} must not have surrounding whitespace")
    return value


def _relative_path(value: Any, path: str, repository_root: pathlib.Path) -> pathlib.Path:
    configured = pathlib.Path(_string(value, path))
    if configured.is_absolute() or ".." in configured.parts:
        raise RalphConfigError(f"{path} must stay inside the repository")
    return repository_root / configured


def _profile(value: Any, path: str) -> AgentProfile:
    values = _mapping(value, path, {"name", "display_name", "reasoning_effort"}, {"engine"})
    engine = _string(values.get("engine", "codex"), f"{path}.engine")
    if engine not in ENGINES:
        raise RalphConfigError(f"{path}.engine must be codex or claude")
    effort = _string(values["reasoning_effort"], f"{path}.reasoning_effort")
    supported_efforts = CLAUDE_REASONING_EFFORTS if engine == "claude" else REASONING_EFFORTS
    if effort not in supported_efforts:
        raise RalphConfigError(
            f"{path}.reasoning_effort must be one of: "
            + ", ".join(sorted(supported_efforts))
        )
    return AgentProfile(
        name=_string(values["name"], f"{path}.name"),
        display_name=_string(values["display_name"], f"{path}.display_name"),
        reasoning_effort=effort,
        engine=engine,
    )


def load_config(
    path: pathlib.Path, repository_root: pathlib.Path, *, require_review: bool = True
) -> RalphConfig:
    """Return one fully validated configuration or raise RalphConfigError."""

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
        {"review_by_default", "caffeinate_by_default"},
    )
    if values["version"] != 1:
        raise RalphConfigError("configuration.version must be 1")

    repository = _string(values["repository"], "configuration.repository")
    if re.fullmatch(r"[^/\s]+/[^/\s]+", repository) is None:
        raise RalphConfigError("configuration.repository must use owner/name format")

    base_branch = _string(values["base_branch"], "configuration.base_branch")
    remote = _string(values["remote"], "configuration.remote")
    for name, value in (("base_branch", base_branch), ("remote", remote)):
        if value.startswith("-") or any(character.isspace() for character in value):
            raise RalphConfigError(f"configuration.{name} is not a safe Git name")

    authors = values["trusted_authors"]
    if not isinstance(authors, list) or not authors:
        raise RalphConfigError("configuration.trusted_authors must be a non-empty list")
    trusted_authors = frozenset(
        _string(author, f"configuration.trusted_authors[{index}]")
        for index, author in enumerate(authors)
    )
    if len(trusted_authors) != len(authors):
        raise RalphConfigError("configuration.trusted_authors must not contain duplicates")

    review_by_default = values.get("review_by_default", False)
    if not isinstance(review_by_default, bool):
        raise RalphConfigError("configuration.review_by_default must be a boolean")
    caffeinate_by_default = values.get("caffeinate_by_default", False)
    if not isinstance(caffeinate_by_default, bool):
        raise RalphConfigError("configuration.caffeinate_by_default must be a boolean")

    if isinstance(values["labels"], dict) and (
        missing := sorted(LABEL_KEYS - set(LATER_LABEL_DEFAULTS) - set(values["labels"]))
    ):
        lines = ", ".join(f'{key} = "{DEFAULT_LABELS[key]}"' for key in missing)
        raise RalphConfigError(
            f"configuration.labels is missing keys: {', '.join(missing)} (add {lines} under [labels])"
        )
    label_values = _mapping(
        values["labels"],
        "configuration.labels",
        LABEL_KEYS - set(LATER_LABEL_DEFAULTS),
        set(LATER_LABEL_DEFAULTS),
    )
    label_values = LATER_LABEL_DEFAULTS | label_values
    labels = {
        key: _string(label_values[key], f"configuration.labels.{key}")
        for key in LABEL_KEYS
    }
    if len(set(labels.values())) != len(labels):
        raise RalphConfigError("configuration.labels values must be unique")

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
                raise RalphConfigError(
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
            raise RalphConfigError(f"configuration.verification.{key} must be a boolean")

    return RalphConfig(
        repository=repository,
        base_branch=base_branch,
        remote=remote,
        trusted_authors=trusted_authors,
        labels=labels,
        models=models,
        backup=backup,
        review_by_default=review_by_default,
        caffeinate_by_default=caffeinate_by_default,
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
