"""`ralph-kit run --caffeinate`: keep the machine from idle-sleeping while the run lasts."""

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


def warn(reason: str) -> None:
    print(f"⚠️ --caffeinate ignored: {reason}; the machine may sleep during the run.", file=sys.stderr)


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
    command = helper_command(platform, os.getpid())
    if command is None:
        warn(f"not supported on {platform}")
        yield
        return
    if shutil.which(command[0]) is None:
        warn(f"{command[0]} not found")
        yield
        return
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
