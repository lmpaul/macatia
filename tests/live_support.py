"""Live sessions through Ralph's real engine command and result parsing, and fixture recording.

Used by the opt-in live suite (`test_live.py`) to start real engine sessions
and, when asked, to refresh the recorded outputs in `fixtures/engines/`.
"""

import json
import pathlib
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date


KIT_ROOT = pathlib.Path(__file__).resolve().parents[1]
ENGINE_FIXTURES = KIT_ROOT / "tests" / "fixtures" / "engines"
sys.path.insert(0, str(KIT_ROOT / "src"))

from ralph_kit.agent import agent_command, session_prompt, write_agent_result  # noqa: E402
from ralph_kit.check import AGENT_CLIS  # noqa: E402
from ralph_kit.commands import RalphError  # noqa: E402
from ralph_kit.config import AgentProfile  # noqa: E402
from ralph_kit.loop_core import OUTCOME_SCHEMA_PATH, RunAdapters, parse_agent_result  # noqa: E402
from ralph_kit.sanitize import sanitized_text  # noqa: E402


FIXTURE_PROMPT = (
    "Do not read or change any file and do not run any command. Reply only with the "
    'structured result: outcome "no_changes", summary "Fixture session: nothing to do.", findings [].'
)
FIXTURE_RESULT = {"outcome": "no_changes", "summary": "Fixture session: nothing to do.", "findings": []}
# Recorded outputs are committed, so they must not leak a machine or a credential.
FORBIDDEN_FIXTURE_MARKERS = (
    "/Users/", "/home/", "/tmp/", "/var/", "/private/", "sk-", "ghp_", "Bearer",
)
MAX_REPORTED_STDOUT = 2000


class LiveSessionFailed(AssertionError):
    """A real engine session did not produce a result Ralph accepts."""


@dataclass(frozen=True)
class LiveSession:
    result: dict
    # What Ralph reads: Claude's stdout envelope, or the file Codex wrote.
    raw_output: str


def run_live_session(engine, model, reasoning_effort, repository_root, *, runner=RunAdapters.run_engine):
    """Run one session exactly as a run does: `agent_command`, then Ralph's result parsing."""
    profile = AgentProfile(name=model, display_name=model, reasoning_effort=reasoning_effort, engine=engine)
    with tempfile.TemporaryDirectory(prefix="ralph-live-") as directory:
        output_path = pathlib.Path(directory) / "result.json"
        arguments = agent_command(
            profile, session_prompt(engine, FIXTURE_PROMPT), repository_root, OUTCOME_SCHEMA_PATH, output_path
        )
        completed = runner(repository_root, arguments)
        if completed.returncode != 0:
            raise LiveSessionFailed(failure_report(engine, completed, "no result accepted from a failed process"))
        try:
            write_agent_result(engine, completed.stdout, output_path)
            result = parse_agent_result(output_path)
        except (ValueError, RalphError) as error:
            raise LiveSessionFailed(failure_report(engine, completed, error)) from error
        raw_output = completed.stdout if engine == "claude" else output_path.read_text(encoding="utf-8")
    return LiveSession(result, raw_output)


def failure_report(engine, completed, error):
    stdout = sanitized_text(completed.stdout or "")[-MAX_REPORTED_STDOUT:]
    return (
        f"{AGENT_CLIS[engine]} exited with code {completed.returncode}: {error}\n"
        f"stderr:\n{sanitized_text(completed.stderr or '').strip() or '(empty)'}\n"
        f"stdout (last {MAX_REPORTED_STDOUT} characters):\n{stdout.strip() or '(empty)'}"
    )


def scratch_repository(directory):
    """An empty committed Git repository, so a session cannot touch real work."""
    root = pathlib.Path(directory)
    for arguments in (
        ["init", "-b", "main"],
        ["-c", "user.name=Ralph Live", "-c", "user.email=ralph-live@example.invalid",
         "commit", "--allow-empty", "-m", "Live session scratch repository"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    return root


def cli_version(engine):
    return subprocess.run(
        [engine, "--version"], check=True, text=True, capture_output=True
    ).stdout.strip()


def record_engine_fixture(engine, session, *, model, reasoning_effort, version):
    """Replace one engine's recorded output and note the CLI version that produced it."""
    for marker in FORBIDDEN_FIXTURE_MARKERS:
        if marker in session.raw_output:
            raise LiveSessionFailed(
                f"{AGENT_CLIS[engine]} output contains {marker!r}; not recorded as a fixture"
            )
    recorded_path = ENGINE_FIXTURES / "recorded.json"
    recorded = json.loads(recorded_path.read_text(encoding="utf-8"))
    entry = recorded[engine]
    (ENGINE_FIXTURES / entry["output"]).write_text(session.raw_output, encoding="utf-8")
    entry.update(version=version, model=model, reasoning_effort=reasoning_effort)
    recorded.update(recorded_at=date.today().isoformat(), prompt=FIXTURE_PROMPT)
    recorded_path.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
