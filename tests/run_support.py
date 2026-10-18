"""In-memory run support: a GitHub fake, scripted sessions, and a real Git repository.

A run-level test builds a `RunRepository`, a `FakeGitHub`, and `ScriptedSessions`,
then starts the run with `start_run`. Git stays real: the repository pushes to a
local bare `origin`. Everything else a run reaches is configured per test.
"""

import contextlib
import copy
import dataclasses
import io
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field


KIT_ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = KIT_ROOT / "tests" / "fixtures"
sys.path.insert(0, str(KIT_ROOT / "src"))

from ralph_kit import loop_core  # noqa: E402
from ralph_kit.child_cycle import SessionResult  # noqa: E402


REPOSITORY = "lmpaul/buyr"
TRUSTED_LOGIN = "lmpaul"
BRANCH = "codex/issue-191"


def git(repository, *arguments):
    return subprocess.run(
        ["git", *arguments], cwd=repository, check=True, text=True, capture_output=True
    ).stdout.strip()


class RunRepository:
    """A temporary checkout on a Ralph branch, with a bare `origin` and the fixture policy.

    The fixture policy has one local gate. By default it always passes; with
    `local_gate_results` ("pass" or "fail" per call, the last repeats) it runs
    the scripted gate in `fixtures/local_gate.py`. With both verification paths
    disabled, no quality policy is installed, so a run that loads it fails.
    """

    def __init__(self, test_case, *, local_gate_results=None, local_checks=True, ci_checks=True):
        directory = tempfile.TemporaryDirectory()
        test_case.addCleanup(directory.cleanup)
        root = pathlib.Path(directory.name)
        self.path = root / "repository"
        self.origin = root / "origin.git"
        self.path.mkdir()
        git(root, "init", "--bare", str(self.origin))
        git(self.path, "init", "-b", "main")
        git(self.path, "config", "user.email", "ralph-test@example.com")
        git(self.path, "config", "user.name", "Ralph Test")
        git(self.path, "remote", "add", "origin", str(self.origin))
        configuration = (FIXTURES / "ralph.toml").read_text(encoding="utf-8")
        flags = "local_checks = true\nci_checks = true"
        assert flags in configuration, "the fixture configuration enables both verification paths"
        configuration = configuration.replace(
            flags, f"local_checks = {str(local_checks).lower()}\nci_checks = {str(ci_checks).lower()}"
        )
        (self.path / "ralph.toml").write_text(configuration, encoding="utf-8")
        (self.path / ".gitignore").write_text(
            "__pycache__/\n*.py[cod]\n.ralph/\n", encoding="utf-8"
        )
        self.local_gate_command = ["true"]
        if local_gate_results is not None:
            results = root / "local-gate-results.json"
            results.write_text(json.dumps(list(local_gate_results)), encoding="utf-8")
            self.local_gate_command = [
                sys.executable, "-I", str(FIXTURES / "local_gate.py"), str(results)
            ]
        if local_checks or ci_checks:
            scripts = self.path / ".github" / "scripts"
            scripts.mkdir(parents=True)
            shutil.copyfile(FIXTURES / "quality.py", scripts / "quality.py")
            (self.path / ".github" / "quality-gates.json").write_text(
                json.dumps(quality_manifest(self.local_gate_command)), encoding="utf-8"
            )
        git(self.path, "add", ".")
        git(self.path, "commit", "-m", "Initial fixture")
        git(self.path, "push", "-u", "origin", "main")
        git(self.path, "checkout", "-b", BRANCH)
        self.base_sha = self.head()

    def head(self):
        return git(self.path, "rev-parse", "HEAD")

    def commits_since(self, base_sha):
        return git(self.path, "rev-list", "--reverse", f"{base_sha}..HEAD").splitlines()

    def is_clean(self):
        return git(self.path, "status", "--porcelain") == ""

    def pushed_head(self, branch=BRANCH):
        return git(self.origin, "rev-parse", f"refs/heads/{branch}")

    def diagnostics(self):
        """The single run's diagnostics directory."""
        runs = sorted((self.path / ".ralph" / "diagnostics").glob("*/"))
        assert len(runs) == 1, runs
        return runs[0]

    def local_gate_heads(self):
        """The head each recorded local gate ran on, in order."""
        return [
            json.loads((evidence / "command.json").read_text())["head_sha"]
            for evidence in sorted((self.diagnostics() / "evidence").glob("*-local-gate"))
        ]

    def ci_evidence(self):
        """The command metadata of each recorded CI failure evidence, in order."""
        return [
            json.loads((evidence / "command.json").read_text())
            for evidence in sorted((self.diagnostics() / "evidence").glob("*-ci"))
        ]


def quality_manifest(local_gate_command):
    return {
        "version": 1,
        "invariants": [],
        "path_groups": {"persistence-and-sync": ["buyr-hono/prisma/**"]},
        "local_gates": [
            {
                "id": "fixture-gate",
                "description": "fixture quality gate",
                "before_agent_review": True,
                "paths": ["**"],
                "command": local_gate_command,
            }
        ],
        "ci": {
            "checks": {
                "canonical": {"name": "Canonical CI", "command": ["quality-gate"]},
                "database": {
                    "name": "PostgreSQL 16 and Redis acceptance",
                    "command": ["quality-gate"],
                },
            },
            "baseline": ["canonical"],
            "conditional": [
                {
                    "id": "persistence-and-sync",
                    "path_groups": ["persistence-and-sync"],
                    "requires": ["database"],
                }
            ],
            "final": ["canonical", "database"],
        },
    }


class RunInterrupted(BaseException):
    """The run's process died; nothing in the run may catch or report it."""


def implementation_evidence(mode, implementation_head, verified_head=None):
    """A trusted implementation evidence comment in the format the given run mode writes."""
    heads = {
        "implementation_head": implementation_head,
        mode.evidence_head_key: verified_head or implementation_head,
    }
    body = f"{mode.implementation_evidence_prefix}{json.dumps(heads, separators=(',', ':'))} -->"
    return {"author": {"login": TRUSTED_LOGIN}, "body": body}


def legacy_implementation_evidence(base_sha, verified_head):
    """A trusted implementation evidence comment from before evidence carried JSON."""
    return {
        "author": {"login": TRUSTED_LOGIN},
        "body": (
            f"Ralph verified implementation commits `{base_sha}..{verified_head}` and "
            f"reviewed head `{verified_head}` with no corrections. Applicable local gates "
            f"and canonical CI are green for exact head `{verified_head}`."
        ),
    }


@dataclass
class PullRequest:
    number: int
    base: str
    head: str
    title: str
    body: str
    is_draft: bool = True
    comments: list[str] = field(default_factory=list)


class FakeGitHub:
    """GitHub as a run sees it, held in memory.

    Issues carry their state, labels, author, and comments; children and
    blockers link them. Every check-runs request answers with the next scripted
    CI round for the requested head; the last round repeats. A round is one
    conclusion for every check, or a mapping from check name to either a
    conclusion or the check-run fields to override (such as another commit's
    `head_sha`, or an empty `details_url` for a check without an Actions job).
    A failed check's job reports `job_head_sha` (by default the checked head)
    and its log is `job_log_output`, or a failed download with `job_log_error`.
    Setting `interrupt_after_comment_on` to an issue number kills the run once,
    right after its next comment on that issue is stored; `after_comment`, when
    set, is called with the issue number and body after each issue comment.
    """

    def __init__(
        self,
        *,
        repository=REPOSITORY,
        login=TRUSTED_LOGIN,
        ci=("success",),
        check_names=("Canonical CI",),
        job_log_output="last CI log line",
        job_log_error=None,
        job_head_sha=None,
        check_annotation="A check failed.",
    ):
        self.repository = repository
        self.login = login
        self.issues = {}
        self.children = {}
        self.blocked_by = {}
        self.pull_requests = []
        self.ci = list(ci)
        self.check_names = tuple(check_names)
        self.job_log_output = job_log_output
        self.job_log_error = job_log_error
        self.job_head_sha = job_head_sha
        self.check_annotation = check_annotation
        self.checked_heads = []
        self.job_log_requests = []
        self.revision = 0
        self.interrupt_after_comment_on = None
        self.after_comment = None

    # Test setup and observation.

    def add_issue(
        self,
        number,
        *,
        parent=None,
        state="open",
        labels=("ready-for-agent",),
        author=TRUSTED_LOGIN,
        comments=(),
        blocked_by=(),
    ):
        self.issues[number] = {
            "number": number,
            "title": f"Issue {number}",
            "body": f"Implement issue {number}.",
            "state": state.upper(),
            "labels": [{"name": label} for label in labels],
            "author": {"login": author},
            "comments": [dict(comment) for comment in comments],
            "updatedAt": self._timestamp(),
        }
        if parent is not None:
            self.children.setdefault(parent, []).append(number)
        self.blocked_by[number] = list(blocked_by)
        return number

    def labels(self, number):
        return {label["name"] for label in self.issues[number]["labels"]}

    def comments(self, number):
        return [comment["body"] for comment in self.issues[number]["comments"]]

    def pull_request(self):
        assert len(self.pull_requests) <= 1, self.pull_requests
        return self.pull_requests[0] if self.pull_requests else None

    def _timestamp(self):
        self.revision += 1
        return f"2026-08-29T00:{self.revision // 60:02d}:{self.revision % 60:02d}Z"

    def _touch(self, number):
        timestamp = self._timestamp()
        self.issues[number]["updatedAt"] = timestamp
        return timestamp

    def _pull(self, number):
        return next(pull for pull in self.pull_requests if pull.number == number)

    # The GitHub adapter interface.

    def repository_name(self):
        return self.repository

    def issue(self, number):
        return copy.deepcopy(self.issues[number])

    def sub_issues(self, parent):
        return [self.issue(number) for number in self.children.get(parent, [])]

    def blockers(self, child):
        return [self.issue(number) for number in self.blocked_by.get(child, [])]

    def comment_on_issue(self, number, body):
        created_at = self._touch(number)
        self.issues[number]["comments"].append(
            {"author": {"login": self.login}, "body": body, "createdAt": created_at}
        )
        if number == self.interrupt_after_comment_on:
            self.interrupt_after_comment_on = None
            raise RunInterrupted(f"interrupted after commenting on #{number}")
        if self.after_comment is not None:
            self.after_comment(number, body)

    def edit_issue_labels(self, number, add, remove):
        names = (self.labels(number) | {add}) - set(remove)
        self.issues[number]["labels"] = [{"name": name} for name in sorted(names)]
        self._touch(number)

    def open_pull_requests(self, head, base):
        return [
            {
                "number": pull.number,
                "isDraft": pull.is_draft,
                "url": f"https://github.test/{self.repository}/pull/{pull.number}",
            }
            for pull in self.pull_requests
            if pull.head == head and pull.base == base
        ]

    def comment_on_pull_request(self, number, body):
        self._pull(number).comments.append(body)

    def create_draft_pull_request(self, base, head, title, body):
        self.pull_requests.append(
            PullRequest(len(self.pull_requests) + 1, base, head, title, body)
        )

    def mark_pull_request_draft(self, number):
        self._pull(number).is_draft = True

    def mark_pull_request_ready(self, number):
        self._pull(number).is_draft = False

    def check_runs(self, head_sha):
        self.checked_heads.append(head_sha)
        ci_round = self.ci.pop(0) if len(self.ci) > 1 else self.ci[0]
        checks = ci_round if isinstance(ci_round, dict) else dict.fromkeys(self.check_names, ci_round)
        return [
            {
                "check_runs": [
                    {
                        "id": index,
                        "name": name,
                        "head_sha": head_sha,
                        "status": "completed",
                        "details_url": (
                            f"https://github.com/{self.repository}/actions/runs/777/job/"
                            f"{1000 + index}"
                        ),
                        **(check if isinstance(check, dict) else {"conclusion": check}),
                    }
                    for index, (name, check) in enumerate(checks.items(), start=1)
                ]
            }
        ]

    def check_run(self, check_id):
        return self._completed(
            ["gh", "api", f"repos/{self.repository}/check-runs/{check_id}"],
            json.dumps({"output": {"summary": self.check_annotation}}),
        )

    def actions_job(self, job_id):
        check_id = int(job_id) - 1000
        return self._completed(
            ["gh", "api", f"repos/{self.repository}/actions/jobs/{job_id}"],
            json.dumps({
                "head_sha": self.job_head_sha or self.checked_heads[-1],
                "check_run_url": (
                    f"https://api.github.com/repos/{self.repository}/check-runs/{check_id}"
                ),
            }),
        )

    def job_log(self, job_id):
        self.job_log_requests.append(job_id)
        arguments = ["gh", "run", "view", "--repo", self.repository, "--job", job_id, "--log"]
        if self.job_log_error is not None:
            return subprocess.CompletedProcess(arguments, 1, "", self.job_log_error)
        return self._completed(arguments, self.job_log_output)

    @staticmethod
    def _completed(arguments, stdout):
        return subprocess.CompletedProcess(arguments, 0, stdout, "")


@dataclass(frozen=True)
class ScriptedResult:
    """One scripted agent session result; `commit` defaults to the outcome needing one.

    The commit adds a new file, or changes `path` when given.
    """

    outcome: str = "committed"
    summary: str = "Did the requested work."
    findings: tuple[str, ...] = ()
    commit: bool | None = None
    path: str | None = None


class ScriptedSessions:
    """Answers each agent session with the next scripted result, committing for real.

    `prompts` keeps the prompt Ralph would give the agent for each session, in order.
    """

    def __init__(self, repository, *sessions):
        self.repository = repository
        self.script = list(sessions)
        self.requests = []
        self.prompts = []

    def run_session(self, request, prompt=""):
        if not self.script:
            raise AssertionError(f"unscripted {request.role} session for #{request.child}")
        session = self.script.pop(0)
        self.requests.append(request)
        self.prompts.append(prompt)
        path = self.repository.path
        before = self.repository.head()
        commit = session.commit if session.commit is not None else session.outcome == "committed"
        if commit:
            name = session.path or (
                f"{request.role.replace(' ', '-')}-{request.child}-{len(self.requests)}.txt"
            )
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_text(f"{session.summary}\n", encoding="utf-8")
            git(path, "add", name)
            git(path, "commit", "-m", f"{request.role} for #{request.child}")
        head = self.repository.head()
        commits = git(path, "rev-list", "--reverse", f"{before}..{head}").splitlines()
        return SessionResult(
            session.outcome, session.summary, list(session.findings), head, commits
        )


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    # Every wait the run asked for, in seconds; tests never actually wait.
    sleeps: list[float]


def start_run(mode, repository, github, sessions, *arguments):
    """Start one run from the repository root with in-memory GitHub and sessions."""

    def child_cycle_adapters(context):
        def run_session(request):
            prompt = loop_core.session_request_prompt(context.state, request)
            return sessions.run_session(request, prompt)

        return dataclasses.replace(
            loop_core.real_child_cycle_adapters(context), run_session=run_session
        )

    sleeps = []
    adapters = loop_core.RunAdapters(
        github=lambda repository_root, name: github,
        child_cycle=child_cycle_adapters,
        sleep=sleeps.append,
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    with (
        contextlib.chdir(repository.path),
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        exit_code = loop_core.run(mode, adapters, [str(argument) for argument in arguments])
    return RunResult(exit_code, stdout.getvalue(), stderr.getvalue(), sleeps)
