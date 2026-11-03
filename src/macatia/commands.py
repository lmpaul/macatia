"""Subprocess execution shared by the run and its GitHub adapter, and the safe stop both raise."""

from __future__ import annotations

import pathlib
import subprocess


class MacatiaError(RuntimeError):
    """A safe terminal stop that must not advance issue state."""


def command(
    repository_root: pathlib.Path,
    arguments: list[str],
    *,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        arguments,
        cwd=repository_root,
        check=False,
        text=True,
        capture_output=capture_output,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or "").strip() or (completed.stdout or "").strip()
        raise MacatiaError(f"command failed ({' '.join(arguments)}): {detail}")
    return completed
