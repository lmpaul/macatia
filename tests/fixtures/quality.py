#!/usr/bin/env python3
"""Select product-quality gates and validate exact-SHA CI evidence."""

from __future__ import annotations

import fnmatch
import json
import pathlib
import subprocess
import sys
from typing import Any, Iterable


DEFAULT_MANIFEST_PATH = pathlib.Path(__file__).parents[1] / "quality-gates.json"
REPOSITORY_ROOT = pathlib.Path(__file__).parents[2]
REQUIRED = "required"
NOT_APPLICABLE = "not_applicable"


def load_manifest(path: pathlib.Path = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("version") != 1:
        raise ValueError("Unsupported quality-gate manifest version")
    checks = manifest.get("ci", {}).get("checks", {})
    referenced = set(manifest["ci"]["baseline"]) | set(manifest["ci"]["final"])
    for rule in manifest["ci"]["conditional"]:
        referenced.update(rule["requires"])
    if unknown := referenced - checks.keys():
        raise ValueError("Quality-gate manifest references unknown checks: " + ", ".join(sorted(unknown)))
    groups = manifest.get("path_groups", {})
    unknown_groups = {
        group
        for rule in [*manifest["local_gates"], *manifest["ci"]["conditional"]]
        for group in rule.get("path_groups", [])
        if group not in groups
    }
    if unknown_groups:
        raise ValueError("Quality-gate manifest references unknown path groups: " + ", ".join(sorted(unknown_groups)))
    return manifest


def _patterns(manifest: dict[str, Any], rule: dict[str, Any]) -> tuple[str, ...]:
    grouped = (pattern for group in rule.get("path_groups", []) for pattern in manifest["path_groups"][group])
    return (*rule.get("paths", []), *grouped)


def rule_matches_paths(manifest: dict[str, Any], rule: dict[str, Any], paths: Iterable[str]) -> bool:
    patterns = _patterns(manifest, rule)
    return any(
        fnmatch.fnmatchcase(path, pattern)
        for path in paths
        for pattern in patterns
    )


def matching_local_gates(manifest: dict[str, Any], changed_paths: Iterable[str]) -> list[dict[str, Any]]:
    paths = tuple(changed_paths)
    return [gate for gate in manifest["local_gates"] if rule_matches_paths(manifest, gate, paths)]


def ci_requirements(manifest: dict[str, Any], changed_paths: Iterable[str], *, final: bool = False) -> dict[str, str]:
    ci = manifest["ci"]
    required_ids = set(ci["final"] if final else ci["baseline"])
    if not final:
        for rule in ci["conditional"]:
            if rule_matches_paths(manifest, rule, changed_paths):
                required_ids.update(rule["requires"])
    return {details["name"]: REQUIRED if gate_id in required_ids else NOT_APPLICABLE for gate_id, details in ci["checks"].items()}


def ci_requires_database(
    manifest: dict[str, Any], changed_paths: Iterable[str], event_name: str
) -> bool:
    if event_name not in {"pull_request", "push"}:
        raise ValueError(f"Unsupported CI event: {event_name}")
    requirements = ci_requirements(
        manifest, changed_paths, final=event_name == "push"
    )
    database_name = manifest["ci"]["checks"]["database"]["name"]
    return requirements[database_name] == REQUIRED


def validate_check_evidence(head_sha: str, check_runs: Iterable[dict[str, Any]], requirements: dict[str, str]) -> dict[str, str]:
    latest: dict[str, dict[str, Any]] = {}
    for check in check_runs:
        if check.get("head_sha") == head_sha and check.get("name") in requirements:
            name = str(check["name"])
            if name not in latest or int(check.get("id", 0)) > int(latest[name].get("id", 0)):
                latest[name] = check
    missing = [name for name, status in requirements.items() if status == REQUIRED and name not in latest]
    non_green = [name for name, status in requirements.items() if status == REQUIRED and name in latest and (latest[name].get("status") != "completed" or latest[name].get("conclusion") != "success")]
    if missing or non_green:
        details = (["missing: " + ", ".join(missing)] if missing else []) + (["not green: " + ", ".join(non_green)] if non_green else [])
        raise RuntimeError("required CI evidence is incomplete (" + "; ".join(details) + ")")
    return {name: "success" if status == REQUIRED else NOT_APPLICABLE for name, status in requirements.items()}


def changed_paths(base_sha: str, head_sha: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_sha}...{head_sha}"],
        cwd=REPOSITORY_ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.splitlines()


def main(arguments: list[str]) -> int:
    if len(arguments) != 7 or arguments[0] != "ci-requires":
        raise ValueError(
            "Usage: quality.py ci-requires --base BASE_SHA --head HEAD_SHA --event EVENT"
        )
    if (
        arguments[1] != "--base"
        or arguments[3] != "--head"
        or arguments[5] != "--event"
    ):
        raise ValueError(
            "Usage: quality.py ci-requires --base BASE_SHA --head HEAD_SHA --event EVENT"
        )
    required = ci_requires_database(
        load_manifest(), changed_paths(arguments[2], arguments[4]), arguments[6]
    )
    print("true" if required else "false")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (ValueError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(2)
