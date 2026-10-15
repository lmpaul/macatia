"""Translate Ralph sessions to CLI commands and normalize their final results."""

from __future__ import annotations

import json
import pathlib

from .config import AgentProfile


def session_prompt(engine: str, prompt: str) -> str:
    if engine != "claude":
        return prompt
    if prompt.startswith("$implement "):
        prompt = "/implement " + prompt.removeprefix("$implement ")
    return prompt


def claude_json_schema(schema_path: pathlib.Path) -> str:
    """Claude Code rejects the draft 2020-12 `$schema` URI that Codex accepts."""
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema.pop("$schema", None)
    return json.dumps(schema)


def agent_command(
    profile: AgentProfile,
    prompt: str,
    repository_root: pathlib.Path,
    schema_path: pathlib.Path,
    output_path: pathlib.Path,
) -> list[str]:
    if profile.engine == "codex":
        return [
            "codex", "exec", "--model", profile.name,
            "-c", f'model_reasoning_effort="{profile.reasoning_effort}"',
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd", str(repository_root),
            "--output-schema", str(schema_path),
            "--output-last-message", str(output_path), prompt,
        ]
    return [
        "claude", "-p", "--model", profile.name,
        "--effort", profile.reasoning_effort,
        "--dangerously-skip-permissions",
        "--output-format", "json",
        "--json-schema", claude_json_schema(schema_path),
        "--append-system-prompt",
        "Read and follow AGENTS.md in the repository and applicable directories. "
        "Work in the current repository; leave pushing and GitHub mutations to Ralph.",
        prompt,
    ]


def write_agent_result(engine: str, stdout: str, output_path: pathlib.Path) -> None:
    """Claude wraps its schema result; Codex already writes the result file."""
    if engine != "claude":
        return
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ValueError("Claude Code did not return valid JSON output") from error
    if not isinstance(envelope, dict):
        raise ValueError("Claude Code output is not an object")
    if envelope.get("is_error"):
        raise ValueError("Claude Code reported an error; inspect retained session output")
    result = envelope.get("structured_output")
    if not isinstance(result, dict):
        raise ValueError("Claude Code did not return a structured_output object")
    output_path.write_text(json.dumps(result), encoding="utf-8")
