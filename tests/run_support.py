"""In-memory run support: a GitHub fake, scripted sessions, and a real Git repository.

A run-level test builds a `RunRepository`, a `FakeGitHub`, and `ScriptedSessions`,
then starts the run with `start_run`, or `start_mode_run` for a given run mode.
Git stays real: the repository pushes to a local bare `origin`. Everything else
a run reaches is configured per test.
"""

import contextlib
import copy
import dataclasses
import io
import json
import os
import pathlib
import re
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
from ralph_kit.commands import RalphError  # noqa: E402
from ralph_kit.github import (  # noqa: E402
    actions_job_command,
    check_run_command,
    job_log_command,
    merged_pull_requests_into,
)


REPOSITORY = "lmpaul/buyr"
TRUSTED_LOGIN = "lmpaul"
BRANCH = "codex/issue-191"


def git(repository, *arguments):
    return subprocess.run(
        ["git", *arguments], cwd=repository, check=True, text=True, capture_output=True
    ).stdout.strip()


# The scripted local gate runs from the repository root and reads its results
# next to the repository, so every copy of a template finds its own.
SCRIPTED_LOCAL_GATE = [
    sys.executable, "-I", str(FIXTURES / "local_gate.py"), "../local-gate-results.json"
]


class RunRepository:
    """A temporary checkout on a Ralph branch, with a bare `origin` and the fixture policy.

    The fixture policy has one local gate. By default it always passes; with
    `local_gate_results` ("pass" or "fail" per call, the last repeats) it runs
    the scripted gate in `fixtures/local_gate.py`. With both verification paths
    disabled, no quality policy is installed, so a run that loads it fails.
    `engines` maps a model profile to the engine it uses, or to None to leave
    the profile without an `engine` key; other profiles use Codex.

    Each setup is built with Git once per test process, then copied for each test.
    """

    templates = {}

    def __init__(
        self, test_case, *, local_gate_results=None, local_checks=True, ci_checks=True, engines=None
    ):
        self.local_gate_command = SCRIPTED_LOCAL_GATE if local_gate_results is not None else ["true"]
        setup = (local_checks, ci_checks, tuple((engines or {}).items()), tuple(self.local_gate_command))
        if setup not in self.templates:
            self.templates[setup] = RepositoryTemplate(
                local_checks, ci_checks, engines or {}, self.local_gate_command
            )
        template = self.templates[setup]
        directory = tempfile.TemporaryDirectory()
        test_case.addCleanup(directory.cleanup)
        root = pathlib.Path(directory.name)
        shutil.copytree(template.root, root, symlinks=True, dirs_exist_ok=True)
        self.path = root / "repository"
        self.origin = root / "origin.git"
        git_config = self.path / ".git" / "config"
        git_config.write_text(
            git_config.read_text(encoding="utf-8").replace(str(template.root), str(root)),
            encoding="utf-8",
        )
        if local_gate_results is not None:
            (root / "local-gate-results.json").write_text(
                json.dumps(list(local_gate_results)), encoding="utf-8"
            )
        self.base_sha = template.base_sha

    def edit_configuration(self, old, new, path="ralph.toml"):
        """Replace text in the configuration at `path` and commit it, keeping the worktree clean."""
        configuration_path = self.path / path
        configuration = configuration_path.read_text(encoding="utf-8")
        assert old in configuration, old
        configuration_path.write_text(configuration.replace(old, new), encoding="utf-8")
        git(self.path, "commit", "-am", "Edit the Ralph configuration")

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

    def session_records(self):
        """The diagnostic record of each agent session, in order."""
        return [
            json.loads((session / "record.json").read_text())
            for session in self.session_directories()
        ]

    def session_directories(self):
        return sorted((self.diagnostics() / "sessions").glob("*/"))

    def ci_evidence(self):
        """The command metadata of each recorded CI failure evidence, in order."""
        return [
            json.loads((evidence / "command.json").read_text())
            for evidence in sorted((self.diagnostics() / "evidence").glob("*-ci"))
        ]


class RepositoryTemplate:
    """One fixture repository and its bare `origin`, built with Git and kept for the test process."""

    def __init__(self, local_checks, ci_checks, engines, local_gate_command):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        path = self.root / "repository"
        origin = self.root / "origin.git"
        path.mkdir()
        git(self.root, "init", "--bare", str(origin))
        git(path, "init", "-b", "main")
        git(path, "config", "user.email", "ralph-test@example.com")
        git(path, "config", "user.name", "Ralph Test")
        git(path, "remote", "add", "origin", str(origin))
        configuration = (FIXTURES / "ralph.toml").read_text(encoding="utf-8")
        flags = "local_checks = true\nci_checks = true"
        assert flags in configuration, "the fixture configuration enables both verification paths"
        configuration = configuration.replace(
            flags, f"local_checks = {str(local_checks).lower()}\nci_checks = {str(ci_checks).lower()}"
        )
        for profile, engine in engines.items():
            configuration = configure_engine(configuration, profile, engine)
        (path / "ralph.toml").write_text(configuration, encoding="utf-8")
        # Run diagnostics are deliberately not ignored here: the run must ignore them itself.
        (path / ".gitignore").write_text("__pycache__/\n*.py[cod]\n", encoding="utf-8")
        if local_checks or ci_checks:
            scripts = path / ".github" / "scripts"
            scripts.mkdir(parents=True)
            shutil.copyfile(FIXTURES / "quality.py", scripts / "quality.py")
            (path / ".github" / "quality-gates.json").write_text(
                json.dumps(quality_manifest(local_gate_command)), encoding="utf-8"
            )
        git(path, "add", ".")
        git(path, "commit", "-m", "Initial fixture")
        git(path, "push", "-u", "origin", "main")
        git(path, "checkout", "-b", BRANCH)
        self.base_sha = git(path, "rev-parse", "HEAD")


def configure_engine(configuration, profile, engine):
    """Set one model profile's engine; a Claude Code profile also gets a Claude model name."""
    header = f"[models.{profile}]"
    start = configuration.index(header)
    end = configuration.find("\n[", start + len(header))
    section = configuration[start:end]
    assert 'engine = "codex"\n' in section, section
    if engine is None:
        section = section.replace('engine = "codex"\n', "")
    else:
        section = section.replace('engine = "codex"', f'engine = "{engine}"')
    if engine == "claude":
        section = re.sub(r'name = "gpt-[^"]+"', f'name = "claude-{profile}"', section)
    return configuration[:start] + section + configuration[end:]


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


def is_child_cycle_start(body):
    return loop_core.CHILD_BASE_MARKER_PREFIX in body


def child_base_marker(child_base, author=TRUSTED_LOGIN):
    """A child cycle start comment recording `child_base`, as Ralph writes it."""
    marker = json.dumps({"child_base": child_base}, separators=(",", ":"))
    body = f"{loop_core.CHILD_BASE_MARKER_PREFIX}{marker} -->\nRalph started a child cycle."
    return {"author": {"login": author}, "body": body}


def reset_marker(author=TRUSTED_LOGIN, created_at=None):
    """A reset marker as a reset leaves it on a child it made ready for an agent again."""
    marker = json.dumps({"previous_state": "implemented"}, separators=(",", ":"))
    body = f"{loop_core.RESET_MARKER_PREFIX}{marker} -->\nA reset made this child ready for an agent again."
    comment = {"author": {"login": author}, "body": body}
    if created_at is not None:
        comment["createdAt"] = created_at
    return comment


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
    state: str = "open"


@dataclass(frozen=True)
class CommitPull:
    """A pull request GitHub associates with a commit: merged, closed unmerged, or open.

    GitHub reports a merge commit SHA for an unmerged pull request too: its test merge.
    """

    number: int
    merge_commit: str
    base: str = "main"
    state: str = "merged"

    def listing(self):
        """The pull request as the commit-to-pull-requests listing returns it."""
        return {
            "number": self.number,
            "state": "open" if self.state == "open" else "closed",
            "base": {"ref": self.base},
            "merged_at": "2026-08-29T00:00:00Z" if self.state == "merged" else None,
            "merge_commit_sha": self.merge_commit,
        }


class FakeGitHub:
    """GitHub as a run sees it, held in memory.

    Issues carry their state, labels, author, and comments; children and
    blockers link them; an issue's parent is the issue it was added as a child
    of, and `parent_error` maps an issue number to the error its parent lookup
    raises instead. Every check-runs request answers with the next scripted
    CI round for the requested head; the last round repeats. A round is one
    conclusion for every check, or a mapping from check name to either a
    conclusion or the check-run fields to override (such as another commit's
    `head_sha`, or an empty `details_url` for a check without an Actions job).
    A round can also be a `RalphError`, raised as the failed `gh` call would.
    A failed check's job reports `job_head_sha` (by default the checked head)
    and its log is `job_log_output`, or a failed download with `job_log_error`.
    Setting `interrupt_after_comment_on` to an issue number kills the run once,
    right after its next comment on that issue is stored (only a comment
    containing `interrupt_after_comment_containing`, when set); `after_comment`, when
    set, is called with the issue number and body after each issue comment;
    `before_check_runs`, when set, is called with the requested head before each
    check-runs request is answered; `after_label_edit`, when set, is called with
    the issue number, the added label, and the removed labels after each label
    edit. Setting `label_edit_error` makes the next label edit raise it, as the
    failed `gh` call would, without changing labels. Closing an issue records
    its reason as `stateReason`; `close_error` maps an issue number to the error
    its next close raises instead, and `after_close`, when set, is called with
    the issue number and reason after each close. Closing a pull request stores its
    comment and marks it closed; `after_pull_request_close`, when set, is called with
    its number after each close. Reading an unknown issue raises
    as `gh` does for an issue that does not exist. A commit is on a branch when
    `on_branch` maps its SHA to True; `compared` keeps each (branch, SHA) asked.
    `commit_pulls` maps a commit SHA to the pull requests GitHub associates with
    it, each a `CommitPull`, or to a `RalphError` its lookup raises;
    `pull_lookups` keeps each (base, SHA) asked.
    `opened_for` keeps the configured repository each run opened GitHub access for.
    The open issues listing answers every open issue in the order they were added,
    or raises `open_issues_error` when set.
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
        self.parent_error = {}
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
        self.interrupt_after_comment_containing = None
        self.after_comment = None
        self.before_check_runs = None
        self.after_label_edit = None
        self.label_edit_error = None
        self.close_error = {}
        self.after_close = None
        self.after_pull_request_close = None
        self.on_branch = {}
        self.compared = []
        self.commit_pulls = {}
        self.pull_lookups = []
        self.opened_for = []
        self.open_issues_error = None

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

    def reports(self, number):
        """The child's comments other than the child cycle start comments."""
        return [body for body in self.comments(number) if not is_child_cycle_start(body)]

    def latest_comment_url(self, number):
        return self.issues[number]["comments"][-1]["url"]

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
        if number not in self.issues:
            raise RalphError(
                f"command failed (gh issue view {number}): "
                f"GraphQL: Could not resolve to an issue or pull request with the number of {number}."
            )
        return copy.deepcopy(self.issues[number])

    def open_issues(self):
        if self.open_issues_error is not None:
            raise self.open_issues_error
        return [
            self.issue(number) for number, issue in self.issues.items() if issue["state"] == "OPEN"
        ]

    def sub_issues(self, parent):
        return [self.issue(number) for number in self.children.get(parent, [])]

    def parent(self, number):
        if number in self.parent_error:
            raise self.parent_error[number]
        self.issue(number)
        for parent, children in self.children.items():
            if number in children:
                return self.issue(parent)
        return None

    def blockers(self, child):
        return [self.issue(number) for number in self.blocked_by.get(child, [])]

    def commit_is_on_branch(self, branch, sha):
        self.compared.append((branch, sha))
        return self.on_branch.get(sha, False)

    def merged_pull_requests(self, base, sha):
        self.pull_lookups.append((base, sha))
        pulls = self.commit_pulls.get(sha, [])
        if isinstance(pulls, RalphError):
            raise pulls
        return merged_pull_requests_into(base, [pull.listing() for pull in pulls])

    def comment_on_issue(self, number, body):
        created_at = self._touch(number)
        self.issues[number]["comments"].append({
            "author": {"login": self.login},
            "body": body,
            "createdAt": created_at,
            "url": f"https://github.test/{self.repository}/issues/{number}#issuecomment-{self.revision}",
        })
        if number == self.interrupt_after_comment_on and (
            self.interrupt_after_comment_containing is None
            or self.interrupt_after_comment_containing in body
        ):
            self.interrupt_after_comment_on = None
            raise RunInterrupted(f"interrupted after commenting on #{number}")
        if self.after_comment is not None:
            self.after_comment(number, body)

    def close_issue(self, number, reason):
        if number in self.close_error:
            raise self.close_error.pop(number)
        self.issues[number]["state"] = "CLOSED"
        self.issues[number]["stateReason"] = reason.upper()
        self._touch(number)
        if self.after_close is not None:
            self.after_close(number, reason)

    def edit_issue_labels(self, number, add, remove):
        if self.label_edit_error is not None:
            error, self.label_edit_error = self.label_edit_error, None
            raise error
        names = (self.labels(number) | {add}) - set(remove)
        self.issues[number]["labels"] = [{"name": name} for name in sorted(names)]
        self._touch(number)
        if self.after_label_edit is not None:
            self.after_label_edit(number, add, remove)

    def open_pull_requests(self, head, base):
        return [
            {
                "number": pull.number,
                "isDraft": pull.is_draft,
                "url": f"https://github.test/{self.repository}/pull/{pull.number}",
            }
            for pull in self.pull_requests
            if pull.head == head and pull.base == base and pull.state == "open"
        ]

    def open_pull_requests_by_title(self, base, titles):
        return [
            {
                "number": pull.number,
                "title": pull.title,
                "head": pull.head,
                "url": f"https://github.test/{self.repository}/pull/{pull.number}",
            }
            for pull in self.pull_requests
            if pull.base == base and pull.title in titles and pull.state == "open"
        ]

    def close_pull_request(self, number, comment):
        pull = self._pull(number)
        pull.comments.append(comment)
        pull.state = "closed"
        if self.after_pull_request_close is not None:
            self.after_pull_request_close(number)

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
        if self.before_check_runs is not None:
            self.before_check_runs(head_sha)
        ci_round = self.ci.pop(0) if len(self.ci) > 1 else self.ci[0]
        if isinstance(ci_round, RalphError):
            raise ci_round
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
            check_run_command(self.repository, check_id),
            json.dumps({"output": {"summary": self.check_annotation}}),
        )

    def actions_job(self, job_id):
        check_id = int(job_id) - 1000
        return self._completed(
            actions_job_command(self.repository, job_id),
            json.dumps({
                "head_sha": self.job_head_sha or self.checked_heads[-1],
                "check_run_url": (
                    f"https://api.github.com/repos/{self.repository}/check-runs/{check_id}"
                ),
            }),
        )

    def job_log(self, job_id):
        self.job_log_requests.append(job_id)
        arguments = job_log_command(self.repository, job_id)
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


@dataclass(frozen=True)
class ScriptedSignal:
    """A scripted session that sends `signal_number` to its own process, as `kill` would."""

    signal_number: int


class ScriptedSessions:
    """Answers each agent session with the next scripted result, committing for real.

    A scripted exception, any `BaseException` such as `KeyboardInterrupt`, is
    raised by its session instead, as a crashing or interrupted adapter would; a
    `ScriptedSignal` signals the run's own process.
    `prompts` keeps the prompt Ralph would give the agent for each session, in order.
    `before_session`, when set, is called with each request before it is answered.
    """

    def __init__(self, repository, *sessions):
        self.repository = repository
        self.script = list(sessions)
        self.requests = []
        self.prompts = []
        self.before_session = None

    def run_session(self, request, prompt=""):
        if not self.script:
            raise AssertionError(f"unscripted {request.role} session for #{request.child}")
        if self.before_session is not None:
            self.before_session(request)
        session = self.script.pop(0)
        self.requests.append(request)
        self.prompts.append(prompt)
        if isinstance(session, BaseException):
            raise session
        if isinstance(session, ScriptedSignal):
            os.kill(os.getpid(), session.signal_number)
            # The run's handler raises before the signal call returns here.
            raise AssertionError(f"signal {session.signal_number} did not interrupt the run")
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
class EngineResult:
    """One scripted engine process: the result it reports and what it leaves in Git.

    `commit` defaults to the outcome needing one; `dirty` leaves an uncommitted
    file. `structured` replaces the reported result object, and with
    `writes_result` false the engine reports no result at all. Claude Code
    prints `stdout` instead of its result envelope when given, and flags the
    envelope as an error with `is_error`. Either engine writes `stderr`.
    """

    outcome: str = "committed"
    summary: str = "Did the requested work."
    findings: tuple[str, ...] = ()
    commit: bool | None = None
    dirty: bool = False
    returncode: int = 0
    structured: dict | None = None
    writes_result: bool = True
    stdout: str | None = None
    is_error: bool = False
    stderr: str = ""


@dataclass(frozen=True)
class EngineCall:
    engine: str
    arguments: list[str]

    @property
    def prompt(self):
        return self.arguments[-1]

    def option(self, flag):
        return self.arguments[self.arguments.index(flag) + 1]


class ScriptedEngine:
    """Stands in for the `codex` and `claude` processes behind Ralph's real session handling.

    Each engine command is answered with the next scripted result: the engine
    commits for real, then writes its result the way that CLI does (Codex to
    its result file, Claude Code in a JSON envelope on stdout). A scripted
    `BaseException`, such as `KeyboardInterrupt`, is raised by its command
    instead, as an interrupted engine process would.
    """

    def __init__(self, repository, *results):
        self.repository = repository
        self.script = list(results)
        self.calls = []

    def engines(self):
        return [call.engine for call in self.calls]

    def run(self, repository_root, arguments):
        assert pathlib.Path(repository_root) == self.repository.path.resolve(), repository_root
        call = EngineCall(arguments[0], list(arguments))
        if not self.script:
            raise AssertionError(f"unscripted {call.engine} session: {call.prompt[:80]!r}")
        session = self.script.pop(0)
        self.calls.append(call)
        if isinstance(session, BaseException):
            raise session
        path = self.repository.path
        commit = session.commit if session.commit is not None else session.outcome == "committed"
        if commit:
            name = f"{call.engine}-session-{len(self.calls)}.txt"
            (path / name).write_text(f"{session.summary}\n", encoding="utf-8")
            git(path, "add", name)
            git(path, "commit", "-m", f"{call.engine} session {len(self.calls)}")
        if session.dirty:
            (path / "uncommitted-agent-output.txt").write_text("unfinished\n", encoding="utf-8")
        result = session.structured or {
            "outcome": session.outcome,
            "summary": session.summary,
            "findings": list(session.findings),
        }
        stdout = ""
        if call.engine == "codex":
            if session.writes_result:
                pathlib.Path(call.option("--output-last-message")).write_text(
                    json.dumps(result), encoding="utf-8"
                )
        else:
            envelope = {"type": "result", "is_error": session.is_error, "result": ""}
            if session.writes_result:
                envelope["structured_output"] = result
            stdout = session.stdout if session.stdout is not None else json.dumps(envelope)
        return subprocess.CompletedProcess(arguments, session.returncode, stdout, session.stderr)


class FakeSleepLock:
    """Stands in for the sleep lock helper: records how often the run took the sleep lock,
    and whether it was held when each agent session ran."""

    def __init__(self):
        self.taken = 0
        self.held = False
        self.held_during_sessions = []

    @contextlib.contextmanager
    def __call__(self):
        self.taken += 1
        self.held = True
        try:
            yield
        finally:
            self.held = False

    def session_started(self):
        self.held_during_sessions.append(self.held)


@dataclass(frozen=True)
class Restart:
    """A restart the run asked of the persistent terminal, and whether it still held the
    sleep lock then."""

    name: str
    command: str
    directory: pathlib.Path
    sleep_lock_held: bool


class FakePersistentTerminal:
    """Stands in for tmux: records each restart instead of replacing the process.

    `unavailability` is why a persistent terminal cannot be used, an `Unavailability`, or
    None when it can; `checked` counts how often the run asked. `existing` are the names of
    the persistent terminals that already exist.
    """

    def __init__(self, sleep_lock, unavailability=None, existing=()):
        self.sleep_lock = sleep_lock
        self.restarts = []
        self.unavailability = unavailability
        self.checked = 0
        self.existing = set(existing)

    def unavailable(self):
        self.checked += 1
        return self.unavailability

    def exists(self, name):
        return name in self.existing

    def restart(self, name, command, directory):
        self.restarts.append(Restart(name, command, directory, self.sleep_lock.held))


class ScriptedContinuation:
    """Answers the continuation choice as the person at the terminal would.

    `answer` is a `Continuation`, or an exception the prompt raises. `asked` keeps the
    persistent terminal name offered each time; `before_answer`, when set, is called as it is asked.
    """

    def __init__(self, answer):
        self.answer = answer
        self.asked = []
        self.before_answer = None

    def __call__(self, name):
        self.asked.append(name)
        if self.before_answer is not None:
            self.before_answer()
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class TerminalOutput(io.StringIO):
    """Output that says it is a terminal."""

    def isatty(self):
        return True


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    # Every wait the run asked for, in seconds; tests never actually wait, and
    # the run's clock advances by them instead.
    sleeps: list[float]
    sleep_lock: FakeSleepLock
    persistent_terminal: FakePersistentTerminal


def start_mode_run(mode, repository, github, sessions, *arguments, **options):
    """`start_run` for the standard or reviewed run `mode`: adds `--review` for a reviewed run."""
    review = ["--review"] if mode.review else []
    return start_run(repository, github, sessions, *review, *arguments, **options)


def start_run(
    repository, github, sessions, *arguments, cwd=None, confirm=None, choose_run_target=None,
    choose_continuation=None, terminal=False, persistent_terminal_unavailability=None,
    existing_persistent_terminals=(),
):
    """Start one run from its command-line `arguments`, with in-memory GitHub and sessions,
    from `cwd` or the repository root.

    `confirm` answers the run's startup question, `choose_run_target` picks among the
    startable run targets, and `choose_continuation` answers the continuation choice,
    as a terminal would; None runs without a terminal. `terminal` makes the output a terminal, which gets the spinners.
    `persistent_terminal_unavailability` is why the fake persistent terminal cannot be used,
    and `existing_persistent_terminals` the names of those that already exist.

    `sessions` is either `ScriptedSessions`, answering each session request, or
    a `ScriptedEngine`, answering each engine command of Ralph's real sessions.
    A command-line usage error ends the run with argparse's exit code.
    """

    def open_github(repository_root, name):
        github.opened_for.append(name)
        return github

    sleep_lock = FakeSleepLock()
    persistent_terminal = FakePersistentTerminal(
        sleep_lock, persistent_terminal_unavailability, existing_persistent_terminals
    )

    def child_cycle_adapters(context):
        def run_session(request):
            sleep_lock.session_started()
            prompt = loop_core.session_request_prompt(context.state, request)
            return sessions.run_session(request, prompt)

        return dataclasses.replace(
            loop_core.real_child_cycle_adapters(context), run_session=run_session
        )

    def run_engine(repository_root, arguments):
        sleep_lock.session_started()
        return sessions.run(repository_root, arguments)

    sleeps = []
    if isinstance(sessions, ScriptedEngine):
        adapters = loop_core.RunAdapters(
            github=open_github,
            child_cycle=loop_core.real_child_cycle_adapters,
            sleep=sleeps.append,
            clock=lambda: sum(sleeps),
            run_engine=run_engine,
            confirm=confirm,
            choose_run_target=choose_run_target,
            keep_awake=sleep_lock,
            persistent_terminal=persistent_terminal,
            choose_continuation=choose_continuation,
        )
    else:
        adapters = loop_core.RunAdapters(
            github=open_github,
            child_cycle=child_cycle_adapters,
            sleep=sleeps.append,
            clock=lambda: sum(sleeps),
            confirm=confirm,
            choose_run_target=choose_run_target,
            keep_awake=sleep_lock,
            persistent_terminal=persistent_terminal,
            choose_continuation=choose_continuation,
        )
    output = TerminalOutput if terminal else io.StringIO
    stdout, stderr = output(), output()
    with (
        contextlib.chdir(cwd or repository.path),
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        try:
            exit_code = loop_core.run(adapters, [str(argument) for argument in arguments])
        except SystemExit as usage_error:
            exit_code = usage_error.code
    return RunResult(
        exit_code, stdout.getvalue(), stderr.getvalue(), sleeps, sleep_lock, persistent_terminal
    )
