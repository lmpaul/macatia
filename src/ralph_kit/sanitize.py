"""Redaction of agent and command output before Ralph stores or republishes it."""

from __future__ import annotations

import re
from typing import Any


def sanitized_diagnostic(raw_output: str) -> str:
    clean = sanitized_text(raw_output)
    lines = [
        line[:300]
        for line in clean.splitlines()
        if line.strip()
    ]
    # Error details often precede a long suite summary. Keep nearby lines first.
    selected: set[int] = set()
    for index, line in enumerate(lines):
        if re.search(r"error|fail|assert|expected|traceback", line, re.IGNORECASE):
            selected.update(range(max(0, index - 1), min(len(lines), index + 5)))
            if len(selected) >= 12:
                break
    excerpt = [lines[index] for index in sorted(selected)] if selected else lines[-6:]
    return "\n".join(excerpt)[:1600] or "No output in this stream."


def sanitized_text(raw_output: str) -> str:
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw_output.replace("\x00", ""))
    clean = clean.replace("```", "'''")
    clean = re.sub(
        r"(?i)\b(password|secret|token|api[_-]?key)\b(\s*[:=]\s*)\S+",
        r"\1\2[REDACTED]",
        clean,
    )
    clean = re.sub(
        r'(?i)(["\']?(?:password|secret|token|api[_-]?key)["\']?\s*:\s*["\']?)[^,\s"\']+',
        r"\1[REDACTED]",
        clean,
    )
    clean = re.sub(r"(?i)Bearer\s+\S+", "Bearer [REDACTED]", clean)
    clean = re.sub(r"([a-z]+://)[^/\s:@]+(?::[^/\s@]+)?@", r"\1[REDACTED]@", clean)
    return clean


def sanitized_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitized_text(value)
    if isinstance(value, dict):
        return {key: sanitized_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitized_value(item) for item in value]
    return value


def format_findings(findings: list[str]) -> str:
    return "\n".join(f"- {sanitized_text(finding)}" for finding in findings)
