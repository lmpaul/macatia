"""Process-test harness: a temporary repository with fake GitHub and agent CLIs."""

import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest


KIT_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY = KIT_ROOT / "src"
FIXTURES = KIT_ROOT / "tests" / "fixtures"
sys.path.insert(0, str(SOURCE_DIRECTORY))


def run(command, *, cwd, env=None, check=True):
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=check,
        text=True,
        capture_output=True,
    )


def write_executable(path, contents):
    path.write_text(textwrap.dedent(contents).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class RalphProcessTestCase(unittest.TestCase):
    # Options inserted after `ralph-kit run`; each loop's tests set their own.
    RUN_OPTIONS = ()
    # Shared across tests: macOS checks each new executable on its first launch,
    # which costs ~0.3s per fake CLI. The fakes read their behavior from the
    # environment, so one copy serves every test.
    _fake_bin_directory = None

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.fixture_root = pathlib.Path(self.temporary_directory.name)
        self.repository = self.fixture_root / "repository"
        self.remote = self.fixture_root / "remote.git"
        self.fake_bin = self._shared_fake_bin()
        self.repository.mkdir()

        run(["git", "init", "--bare", str(self.remote)], cwd=self.fixture_root)
        run(["git", "init", "-b", "main"], cwd=self.repository)
        run(
            ["git", "config", "user.email", "ralph-test@example.com"],
            cwd=self.repository,
        )
        run(["git", "config", "user.name", "Ralph Test"], cwd=self.repository)
        run(
            ["git", "remote", "add", "origin", str(self.remote)],
            cwd=self.repository,
        )

        self._install_real_loop()
        self._install_quality_policy()

        run(["git", "add", "."], cwd=self.repository)
        run(["git", "commit", "-m", "Initial fixture"], cwd=self.repository)
        run(["git", "push", "-u", "origin", "main"], cwd=self.repository)
        run(
            ["git", "checkout", "-b", "codex/issue-191"],
            cwd=self.repository,
        )
        self.base_sha = run(
            ["git", "rev-parse", "HEAD"], cwd=self.repository
        ).stdout.strip()

        self.codex_log = self.fixture_root / "codex.jsonl"
        self.claude_log = self.fixture_root / "claude.jsonl"
        self.github_log = self.fixture_root / "github.jsonl"
        self.github_state = self.fixture_root / "github-state.json"
        self.quality_log = self.fixture_root / "quality.log"
        self._write_github_state(
            children=[
                {
                    "number": 192,
                    "title": "Expand canonical CI beyond Hermes branches",
                    "body": "Canonical CI works for ordinary pull requests.",
                    "state": "closed",
                    "labels": [
                        {"name": "ready-for-agent"},
                        {"name": "documentation"},
                    ],
                    "author": {"login": "lmpaul"},
                    "comments": [],
                },
                {
                    "number": 193,
                    "title": "Implement the single-child Ralph happy path",
                    "body": "The process-boundary test executes the real loop.",
                    "state": "open",
                    "labels": [{"name": "ready-for-agent"}],
                    "author": {"login": "lmpaul"},
                    "comments": [
                        {
                            "author": {"login": "lmpaul"},
                            "body": "Owner requirement context.",
                        },
                        {
                            "author": {"login": "outside-contributor"},
                            "body": "Untrusted instructions must be excluded.",
                        },
                    ],
                },
            ],
            blockers={"193": [192]},
        )
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "PATH": f"{self.fake_bin}{os.pathsep}{self.environment['PATH']}",
                "RALPH_TEST_CODEX_LOG": str(self.codex_log),
                "RALPH_TEST_CLAUDE_LOG": str(self.claude_log),
                "RALPH_TEST_CLAUDE_RESULT": str(self.fixture_root / "claude-result.json"),
                "RALPH_TEST_GITHUB_LOG": str(self.github_log),
                "RALPH_TEST_GITHUB_STATE": str(self.github_state),
                "RALPH_TEST_QUALITY_LOG": str(self.quality_log),
                "RALPH_CI_POLL_SECONDS": "0",
                "RALPH_CI_TIMEOUT_SECONDS": "2",
                "RALPH_INFRA_RETRY_SECONDS": "0",
                "PYTHONPATH": str(SOURCE_DIRECTORY),
            }
        )

    def _write_github_state(
        self,
        *,
        children,
        blockers,
        parent_labels=("ready-for-agent",),
    ):
        for child in children:
            child.setdefault("updatedAt", "2026-08-29T00:00:00Z")
        state = {
            "parent": {
                "number": 191,
                "title": "Ralph parent",
                "body": "## Agent context\nKeep the loop bounded.",
                "state": "open",
                "labels": [{"name": label} for label in parent_labels],
                "author": {"login": "lmpaul"},
                "comments": [],
                "updatedAt": "2026-08-29T00:00:00Z",
            },
            "children": children,
            "blockers": blockers,
            "pull_request": None,
            "revision": 0,
        }
        self.github_state.write_text(json.dumps(state), encoding="utf-8")

    def _child(
        self,
        number,
        *,
        state="open",
        labels=("ready-for-agent",),
        comments=(),
    ):
        return {
            "number": number,
            "title": f"Child {number}",
            "body": f"Implement child {number}.",
            "state": state,
            "labels": [{"name": label} for label in labels],
            "author": {"login": "lmpaul"},
            "comments": list(comments),
        }

    def _install_real_loop(self):
        shutil.copyfile(FIXTURES / "ralph.toml", self.repository / "ralph.toml")

    def _install_quality_policy(self):
        (self.repository / ".gitignore").write_text(
            "__pycache__/\n*.py[cod]\n.ralph/\n", encoding="utf-8"
        )
        scripts = self.repository / ".github" / "scripts"
        scripts.mkdir(parents=True)
        shutil.copyfile(
            FIXTURES / "quality.py",
            scripts / "quality.py",
        )
        manifest = {
            "version": 1,
            "invariants": [],
            "path_groups": {
                "persistence-and-sync": ["buyr-hono/prisma/**"]
            },
            "local_gates": [
                {
                    "id": "fixture-gate",
                    "description": "fixture quality gate",
                    "before_agent_review": True,
                    "paths": ["**"],
                    "command": ["quality-gate"],
                }
            ],
            "ci": {
                "checks": {
                    "canonical": {
                        "name": "Canonical CI",
                        "command": ["quality-gate"],
                    },
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
        (self.repository / ".github" / "quality-gates.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    def _shared_fake_bin(self):
        owner = RalphProcessTestCase
        if owner._fake_bin_directory is None:
            owner._fake_bin_directory = tempfile.TemporaryDirectory()
            self.fake_bin = pathlib.Path(owner._fake_bin_directory.name)
            self._install_fakes()
            self._install_fake_claude()
        return pathlib.Path(owner._fake_bin_directory.name)

    def _install_fakes(self):
        write_executable(
            self.fake_bin / "codex",
            r'''
            #!/usr/bin/env python3
            import json
            import os
            import pathlib
            import re
            import subprocess
            import sys

            arguments = sys.argv[1:]
            prompt = arguments[-1]
            log_path = pathlib.Path(os.environ["RALPH_TEST_CODEX_LOG"])
            call_index = len(log_path.read_text().splitlines()) if log_path.exists() else 0
            with log_path.open("a", encoding="utf-8") as log:
                log.write(json.dumps({"arguments": arguments, "prompt": prompt}) + "\n")

            model = arguments[arguments.index("--model") + 1]
            issue_number = int(re.search(r"issue #(\d+)", prompt).group(1))
            is_correction = (
                "Repair the failing local gate" in prompt
                or "Repair the failing CI check" in prompt
                or "Complete the remaining requirements" in prompt
            )
            outcome = os.environ.get("RALPH_TEST_AGENT_OUTCOME", "committed")
            if (
                is_correction
                and os.environ.get("RALPH_TEST_CI_CORRECTION_NO_CHANGES") == "1"
            ):
                outcome = "no_changes"
            if is_correction and os.environ.get("RALPH_TEST_CORRECTION_OUTCOME"):
                outcome = os.environ["RALPH_TEST_CORRECTION_OUTCOME"]
            results = json.loads(os.environ.get("RALPH_TEST_AGENT_RESULTS", "[]"))
            scripted_result = results[call_index] if call_index < len(results) else {}
            outcome = scripted_result.get("outcome", outcome)
            creates_commit = scripted_result.get("commit", outcome == "committed")
            if model == "gpt-5.6-terra":
                if (
                    creates_commit
                    and os.environ.get("RALPH_TEST_TERRA_SKIPS_COMMIT") != "1"
                ):
                    filename = (
                        f"repair-{issue_number}.txt"
                        if is_correction
                        else f"feature-{issue_number}.txt"
                    )
                    contents = (
                        f"repair {call_index}\n"
                        if is_correction
                        else "implemented\n"
                    )
                    target = pathlib.Path(filename)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(contents, encoding="utf-8")
                summary = (
                    "Repaired the failing local gate."
                    if is_correction
                    else "Implemented the requested child issue."
                )
                message = (
                    f"Repair child issue {issue_number}"
                    if is_correction
                    else f"Implement child issue {issue_number}"
                )
            elif model == "gpt-5.6-sol":
                quality_log = pathlib.Path(os.environ["RALPH_TEST_QUALITY_LOG"])
                quality_heads = (
                    quality_log.read_text(encoding="utf-8").splitlines()
                    if quality_log.exists()
                    else []
                )
                current_head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.strip()
                if (
                    outcome in {"committed", "no_changes", "needs_correction"}
                    and (outcome != "no_changes" or quality_heads)
                    and (
                    not quality_heads or quality_heads[-1] != current_head
                    )
                ):
                    raise SystemExit("Sol started before local gates passed")
                if creates_commit:
                    pathlib.Path(f"review-{issue_number}.txt").write_text(
                        f"corrected {call_index}\n", encoding="utf-8"
                    )
                summary = "Reviewed the exact diff and committed one correction."
                message = f"Correct reviewed implementation {issue_number}"
            else:
                raise SystemExit(f"unexpected model: {model}")

            if creates_commit and not (
                model == "gpt-5.6-terra"
                and os.environ.get("RALPH_TEST_TERRA_SKIPS_COMMIT") == "1"
            ):
                subprocess.run(["git", "add", "."], check=True)
                subprocess.run(["git", "commit", "-m", message], check=True)
            output_path = pathlib.Path(
                arguments[arguments.index("--output-last-message") + 1]
            )
            if os.environ.get("RALPH_TEST_INVALID_OUTPUT") != "1":
                reported_summary = os.environ.get("RALPH_TEST_SUMMARY", summary)
                if model == "gpt-5.6-sol":
                    reported_summary = os.environ.get(
                        "RALPH_TEST_SOL_SUMMARY", reported_summary
                    )
                output_path.write_text(
                    json.dumps({
                        "outcome": outcome,
                        "summary": reported_summary,
                        "findings": scripted_result.get("findings", []),
                    }),
                    encoding="utf-8",
                )
            if os.environ.get("RALPH_TEST_LEAVE_DIRTY") == "1":
                pathlib.Path("uncommitted-agent-output.txt").write_text(
                    "dirty\n", encoding="utf-8"
                )
            if os.environ.get("RALPH_TEST_CODEX_NONZERO") == "1":
                raise SystemExit("simulated interrupted Codex client")
            '''
        )
        write_executable(
            self.fake_bin / "gh",
            r'''
            #!/usr/bin/env python3
            import json
            import os
            import pathlib
            import subprocess
            import sys

            arguments = sys.argv[1:]
            state_path = pathlib.Path(os.environ["RALPH_TEST_GITHUB_STATE"])
            state = json.loads(state_path.read_text(encoding="utf-8"))

            def save_state():
                state_path.write_text(json.dumps(state), encoding="utf-8")

            def touch(target):
                state["revision"] += 1
                timestamp = f"2026-08-29T00:00:{state['revision']:02d}Z"
                target["updatedAt"] = timestamp
                return timestamp

            def issue(number):
                if number == state["parent"]["number"]:
                    return state["parent"]
                return next(
                    child for child in state["children"]
                    if child["number"] == number
                )

            with pathlib.Path(os.environ["RALPH_TEST_GITHUB_LOG"]).open(
                "a", encoding="utf-8"
            ) as log:
                log.write(json.dumps(arguments) + "\n")

            if arguments[:2] == ["run", "view"]:
                print("AssertionError: expected 42, received 0; token=ci-secret")
                print("\n".join("CI progress " + str(i) for i in range(100)))
                print("last CI log line")
            elif arguments[0] == "api" and "/actions/jobs/" in arguments[1]:
                head = subprocess.run(["git", "rev-parse", "HEAD"], check=True, text=True, capture_output=True).stdout.strip()
                check_id = int(arguments[1].rsplit("/", 1)[1]) - 1000
                print(json.dumps({
                    "head_sha": head,
                    "check_run_url": f"https://api.github.com/repos/lmpaul/buyr/check-runs/{check_id}",
                    "steps": [{"name": "Run tests", "conclusion": "failure"}],
                }))
            elif arguments[:2] == ["repo", "view"]:
                print(json.dumps({
                    "nameWithOwner": os.environ.get(
                        "RALPH_TEST_REPOSITORY", "lmpaul/buyr"
                    )
                }))
            elif arguments[:2] == ["issue", "view"]:
                number = int(arguments[2])
                print(json.dumps(issue(number)))
            elif arguments[0] == "api":
                endpoint = arguments[1]
                if endpoint.endswith("/issues/191/sub_issues"):
                    children = state["children"]
                    override = os.environ.get("RALPH_TEST_CHILD_LABELS")
                    if override:
                        for child in children:
                            if child["number"] == 193:
                                child["labels"] = [
                                    {"name": name} for name in override.split(",")
                                ]
                    print(json.dumps(children))
                elif endpoint.endswith("/dependencies/blocked_by"):
                    number = endpoint.split("/issues/", 1)[1].split("/", 1)[0]
                    print(json.dumps([
                        issue(blocker_number)
                        for blocker_number in state["blockers"].get(number, [])
                    ]))
                elif "/commits/" in endpoint and endpoint.endswith("/check-runs"):
                    head = endpoint.split("/commits/", 1)[1].split("/", 1)[0]
                    actual = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        check=True,
                        text=True,
                        capture_output=True,
                    ).stdout.strip()
                    if head != actual:
                        raise SystemExit("CI requested for a stale head")
                    responses_path = state_path.with_suffix(".ci-responses")
                    responses = int(
                        responses_path.read_text() if responses_path.exists() else "0"
                    )
                    responses_path.write_text(str(responses + 1), encoding="utf-8")
                    outcomes = os.environ.get(
                        "RALPH_TEST_CI_OUTCOMES", "success"
                    ).split(",")
                    outcome = outcomes[min(responses, len(outcomes) - 1)]
                    print(json.dumps({
                        "check_runs": [
                            {
                                "id": 1,
                                "details_url": "https://github.com/lmpaul/buyr/actions/runs/777/job/1001",
                                "name": "Canonical CI",
                                "head_sha": head,
                                "status": "completed",
                                "conclusion": outcome,
                            }
                        ]
                    }))
                else:
                    raise SystemExit(f"unexpected api endpoint: {endpoint}")
            elif arguments[:2] == ["pr", "list"]:
                pull_request = state["pull_request"]
                print(json.dumps([pull_request] if pull_request else []))
            elif arguments[:2] == ["pr", "create"]:
                state["pull_request"] = {
                    "number": 1,
                    "isDraft": True,
                    "url": "https://github.test/lmpaul/buyr/pull/1",
                }
                save_state()
                print("https://github.test/lmpaul/buyr/pull/1")
            elif arguments[:2] == ["pr", "ready"]:
                state["pull_request"]["isDraft"] = "--undo" in arguments
                save_state()
            elif arguments[:2] == ["pr", "comment"]:
                pass
            elif arguments[:2] == ["issue", "comment"]:
                number = int(arguments[2])
                body = arguments[arguments.index("--body") + 1]
                target = issue(number)
                created_at = touch(target)
                target["comments"].append({
                    "author": {"login": "lmpaul"},
                    "body": body,
                    "createdAt": created_at,
                })
                save_state()
            elif arguments[:2] == ["issue", "edit"]:
                target = issue(int(arguments[2]))
                names = {label["name"] for label in target["labels"]}
                if "--add-label" in arguments:
                    names.add(arguments[arguments.index("--add-label") + 1])
                if "--remove-label" in arguments:
                    names.difference_update(
                        arguments[arguments.index("--remove-label") + 1].split(",")
                    )
                target["labels"] = [{"name": name} for name in sorted(names)]
                touch(target)
                save_state()
            else:
                raise SystemExit(f"unexpected gh invocation: {arguments}")
            '''
        )
        write_executable(
            self.fake_bin / "quality-gate",
            r'''
            #!/usr/bin/env python3
            import json
            import os
            import pathlib
            import subprocess

            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            with pathlib.Path(os.environ["RALPH_TEST_QUALITY_LOG"]).open(
                "a", encoding="utf-8"
            ) as log:
                log.write(head + "\n")
            quality_log = pathlib.Path(os.environ["RALPH_TEST_QUALITY_LOG"])
            failures_path = quality_log.with_suffix(".failures")
            failures = int(failures_path.read_text() if failures_path.exists() else "0")
            failures_path.write_text(str(failures + 1), encoding="utf-8")
            if failures < int(os.environ.get("RALPH_TEST_FAIL_GATE_COUNT", "0")):
                print("AssertionError: expected local value 42, received 0")
                print("\n".join("local progress " + str(i) for i in range(100)))
                print("last local log line")
                raise SystemExit(
                    'fixture gate failed: password=super-secret '
                    'token: "hidden-token" Bearer bearer-secret '
                    'https://user:password@example.test ```escaped fence'
                )
            '''
        )

    def _json_lines(self, path):
        return [json.loads(line) for line in path.read_text().splitlines()]

    def _run_loop(self, *arguments, cwd=None, environment=None):
        return run(
            [sys.executable, "-m", "ralph_kit", "run", *self.RUN_OPTIONS, *arguments],
            cwd=cwd or self.repository,
            env=environment or self.environment,
            check=False,
        )

    def _configure_verification(self, *, ci_checks, local_checks):
        config_path = self.repository / "ralph.toml"
        contents = config_path.read_text(encoding="utf-8")
        contents = contents.replace('ci_checks = true\n', f'ci_checks = {str(ci_checks).lower()}\n')
        contents = contents.replace(
            'local_checks = true\n',
            f'local_checks = {str(local_checks).lower()}\n',
        )
        config_path.write_text(contents, encoding="utf-8")
        run(["git", "add", "ralph.toml"], cwd=self.repository)
        run(
            [
                "git",
                "commit",
                "-m",
                f"Configure Ralph CI checks {ci_checks} and local checks {local_checks}",
            ],
            cwd=self.repository,
        )
        self.base_sha = run(
            ["git", "rev-parse", "HEAD"], cwd=self.repository
        ).stdout.strip()

    def _install_fake_claude(self):
        # Share the simulated Git effects, but exercise Claude's real CLI envelope.
        source = (self.fake_bin / "codex").read_text()
        source = source.replace('os.environ["RALPH_TEST_CODEX_LOG"]', 'os.environ["RALPH_TEST_CLAUDE_LOG"]')
        source = source.replace(
            'model = arguments[arguments.index("--model") + 1]',
            'if os.environ.get("RALPH_TEST_CLAUDE_CLI_ERROR") == "1":\n'
            '    print("Error: --json-schema is not a valid JSON Schema", file=sys.stderr)\n'
            '    sys.exit(1)\n'
            'model = arguments[arguments.index("--model") + 1]\n'
            'model = {"claude-implementation": "gpt-5.6-terra", "claude-review": "gpt-5.6-sol"}.get(model, model)',
        )
        source = source.replace(
            'arguments[arguments.index("--output-last-message") + 1]',
            'os.environ["RALPH_TEST_CLAUDE_RESULT"]',
        )
        source = source.replace(
            'subprocess.run(["git", "commit", "-m", message], check=True)',
            'subprocess.run(["git", "commit", "-m", message], check=True, capture_output=True)',
        )
        source += '''
if os.environ.get("RALPH_TEST_CLAUDE_INVALID_JSON") == "1":
    print("invalid JSON")
else:
    result = json.loads(output_path.read_text()) if output_path.exists() else None
    if os.environ.get("RALPH_TEST_CLAUDE_RESULT_OVERRIDE"):
        result = json.loads(os.environ["RALPH_TEST_CLAUDE_RESULT_OVERRIDE"])
    print(json.dumps({
        "type": "result",
        "is_error": os.environ.get("RALPH_TEST_CLAUDE_ERROR") == "1",
        "structured_output": result,
    }))
'''
        write_executable(self.fake_bin / "claude", source)

    def _configure_engines(self, **engines):
        path = self.repository / "ralph.toml"
        contents = path.read_text()
        for role, engine in engines.items():
            header = f"[models.{role}]"
            start = contents.index(header)
            end = contents.find("\n[", start + len(header))
            if end == -1:
                end = len(contents)
            section = contents[start:end].replace('engine = "codex"', f'engine = "{engine}"')
            if engine == "claude":
                section = section.replace('name = "gpt-5.6-terra"', 'name = "claude-implementation"')
                section = section.replace('name = "gpt-5.6-sol"', 'name = "claude-review"')
            contents = contents[:start] + section + contents[end:]
        path.write_text(contents)
        run(["git", "add", "ralph.toml"], cwd=self.repository)
        run(["git", "commit", "-m", "Configure agent engines"], cwd=self.repository)
        self.base_sha = run(["git", "rev-parse", "HEAD"], cwd=self.repository).stdout.strip()

    def assert_codex_did_not_start(self):
        self.assertFalse(self.codex_log.exists())
        self.assertFalse(self.claude_log.exists())

    def _diagnostic_records(self):
        runs = sorted((self.repository / ".ralph" / "diagnostics").glob("*/"))
        self.assertEqual(len(runs), 1)
        return [
            json.loads(path.read_text())
            for path in sorted((runs[0] / "sessions").glob("*/record.json"))
        ]
