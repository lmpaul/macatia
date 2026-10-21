"""The sleep lock: keep the machine from idle-sleeping while a run lasts, for `--caffeinate`
or `caffeinate_by_default`."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys


WHY = "ralph-kit run in progress"
# Windows SetThreadExecutionState flags.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def helper_command(platform: str, pid: int) -> list[str] | None:
    """The helper process that holds the sleep lock until process pid exits, or None."""
    if platform == "darwin":
        return ["caffeinate", "-i", "-w", str(pid)]
    if platform.startswith("linux"):
        # tail --pid ends the inhibitor even when ralph-kit is killed without cleanup.
        return [
            "systemd-inhibit",
            "--what=idle:sleep",
            "--who=ralph-kit",
            f"--why={WHY}",
            "--mode=block",
            "tail",
            f"--pid={pid}",
            "-f",
            "/dev/null",
        ]
    return None


def availability(platform: str = sys.platform) -> tuple[str, str | None]:
    """What holds the sleep lock on this platform, and why this machine cannot provide it, or None."""
    if platform == "win32":
        return "SetThreadExecutionState", None
    command = helper_command(platform, os.getpid())
    if command is None:
        return "", f"not supported on {platform}"
    if shutil.which(command[0]) is None:
        return command[0], f"{command[0]} not found"
    return command[0], None


def warn(reason: str) -> None:
    # Neutral wording: the flag or the setting may have asked for the sleep lock.
    print(f"⚠️ No sleep lock: {reason}; the machine may sleep during the run.", file=sys.stderr)


@contextlib.contextmanager
def windows_execution_state():
    import ctypes

    kernel32 = ctypes.windll.kernel32
    if not kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
        warn("SetThreadExecutionState failed")
        yield
        return
    try:
        yield
    finally:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)


@contextlib.contextmanager
def keep_awake(platform: str = sys.platform):
    """Hold a system sleep lock for the duration of the block; warn and continue when unavailable."""
    if platform == "win32":
        with windows_execution_state():
            yield
        return
    _, problem = availability(platform)
    if problem:
        warn(problem)
        yield
        return
    command = helper_command(platform, os.getpid())
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )
    try:
        # A helper that cannot take the lock (no logind, for example) exits at once.
        _, stderr = process.communicate(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    else:
        warn(f"{command[0]} exited with {process.returncode}: {stderr.strip()}".rstrip(": "))
        yield
        return
    try:
        yield
    finally:
        process.terminate()
        process.communicate()
