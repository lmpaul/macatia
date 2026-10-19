# ralph-kit

Ralph runs a loop of coding agents (Codex or Claude Code) over a parent GitHub
issue, one runnable child (a GitHub sub-issue) at a time: implement, verify,
correct, publish a draft PR. A human reviews and merges. The project vocabulary is
defined in [GLOSSARY.md](GLOSSARY.md).

`ralph-kit` is installed once per machine and run from any repository. Each
repository only keeps its own `ralph.toml` (plus, when verification is enabled,
its quality module and manifest).

## Install

Requires Python 3.11+ (uv can provide it), Git, an authenticated `gh` 2.70.0 or
newer, and the CLI of each configured agent engine.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh        # once per machine
uv tool install git+https://github.com/lmpaul/ralph-kit # installs `ralph-kit` on PATH
uv tool upgrade ralph-kit                               # pick up new versions
```

Pin a release with `git+https://github.com/lmpaul/ralph-kit@v0.2.0`.

## Use in a repository

From the repository root:

```sh
ralph-kit init                 # writes ./ralph.toml (detects what it can, asks the rest)
ralph-kit check                # readiness check (offers to create missing labels)
ralph-kit run 123              # standard run: implement the children of parent issue #123
ralph-kit run 123 10           # same, with a run budget of 10 agent sessions
ralph-kit run --review 123     # reviewed run: adds an independent review session per child
ralph-kit run --caffeinate 123 # keep the machine awake until the run ends
```

`--caffeinate` holds an idle-sleep lock for the run: `caffeinate -i` on macOS,
`systemd-inhibit --what=idle:sleep` on Linux, `SetThreadExecutionState` on Windows.
Where none is available, the run warns and continues. The display may still turn off.

Commit `ralph.toml` (and `.gitignore` when `init` changed it) and run on a clean, dedicated branch (never the base branch).

`init` first requires `git`, an authenticated `gh` 2.70.0 or newer and at least one agent CLI
(`claude` or `codex`), and stops before any question otherwise. `check` repeats
these requirements, requiring the CLI of each configured engine, so it is the command
to run when reusing a committed `ralph.toml` on another machine.
`init` then reads the GitHub repository, default branch, remote and current `gh`
account, and enables local gates and CI checks when `.github/scripts/quality.py` and
`.github/quality-gates.json` exist. It asks only for what it could not detect,
plus the agent engine, models and reasoning efforts; lists are navigated with
the arrow keys. Only installed agent CLIs are offered. Codex models and their supported efforts come from your account's
catalog (`codex debug models`, with a free-text fallback); Claude Code has no
model listing, so `init` offers its documented aliases (`fable`, `opus`,
`sonnet`, `haiku`), which always follow the latest version. Without a terminal
(agents, CI) it asks nothing and leaves placeholders that `ralph-kit check` reports.
`--config path/to/other.toml` selects another configuration. Ralph writes run
diagnostics to `.ralph/`. Unless Git already ignores it, `init` appends `.ralph/` to the
repository's `.gitignore` (creating the file if needed) and says so. Commit it with `ralph.toml`;
`check` reports `.ralph/` when Git does not ignore it.

## Develop ralph-kit

```sh
git clone git@github.com:lmpaul/ralph-kit.git && cd ralph-kit
uv tool install -e .                          # `ralph-kit` now runs this checkout
uv run python -m unittest discover -s tests   # offline tests (~35 s), with dependencies
```

The default suite is offline: it never starts `ralph-kit run` as a subprocess,
launches a real agent, or calls GitHub. Each rule is tested at one level:

- **Child cycle rules** (correction limit, review session order, handoff on
  missing commits, run budget checks inside a cycle): `tests/test_child_cycle.py`,
  in memory through the child cycle adapters.
- **Run rules** (child selection, blockers, deadlock, recovery, publication,
  parent completion, infrastructure retries, startup and trust):
  `tests/test_run.py`, a whole run in memory through injected run adapters.
  `tests/run_support.py` provides a GitHub fake, scripted sessions or engine
  processes, and a real temporary Git repository with a bare `origin`.
- **Engine translation** (the CLI command built for each session, and the
  parsing of each engine's final result): `tests/test_agent.py`, pure functions
  against outputs recorded from the real CLIs in `tests/fixtures/engines/`.
- **Readiness check** (including the engine flag check): `tests/test_check.py`,
  with injected executable lookup and command runner. `tests/test_init.py`
  covers `ralph-kit init` the same way.
- **Live suite**: the only proof of compatibility with the real `gh`, `codex`,
  and `claude`: `tests/test_live.py`, skipped unless `RALPH_LIVE=1` (see below).

### Live suite

Run it before a release and after upgrading `gh`, `codex` or `claude`. It needs
an authenticated `gh` and engine CLIs, and spends a few tokens, so it is not part
of the default suite, local gates or CI:

```sh
RALPH_LIVE=1 uv run python -m unittest discover -s tests -p test_live.py -v
```

It makes read-only `gh` calls of each shape a run parses (repository, children,
blockers, issue details, open pull requests, check runs, and the Actions job, job
log and check run behind a check) through Ralph's GitHub adapter, against the
children of parent issue #5 in this repository (`RALPH_LIVE_PARENT` selects another
parent issue with children and blockers) and the check runs of its default branch
head. It then starts one Claude Code and one Codex session in a scratch Git
repository, with a trivial prompt, Ralph's real engine command and output schema,
and checks that Ralph's result parsing accepts the result. A failed session
reports the engine's exit code, stderr and stdout. `RALPH_LIVE_CLAUDE_MODEL`
(default `haiku`) and `RALPH_LIVE_CODEX_MODEL` (default `gpt-5.6-luna`) select the
models. Without `RALPH_LIVE=1` its tests are skipped; `-v` shows the skip reason.

To refresh the recorded engine outputs in `tests/fixtures/engines/` (after an
engine upgrade), add `RALPH_LIVE_REFRESH=1`. Each passing session replaces its
engine's output and records the CLI version, model and date in `recorded.json`;
commit the changed fixtures only when the whole live suite and the default suite
pass:

```sh
RALPH_LIVE=1 RALPH_LIVE_REFRESH=1 uv run python -m unittest discover -s tests -p test_live.py -v
```

## The loop: `run` and `run --review`

Each run executes the loop over the parent issue's runnable children.
`ralph-kit run` starts a standard run, with no review session.
`ralph-kit run --review` starts a reviewed run: the same loop, plus a review
session for each child once its local gates pass, repeated after each issue
correction that review causes. Once a child is selected and its issue trusted,
Ralph replaces its `ready-for-agent` label with `agent-working` before the first
agent session; if that label write fails, the run stops before any session. The
child keeps `agent-working` throughout its child cycle.

Ralph then posts a trusted comment on the child naming its child base, the
commit its implementation evidence, review session diff, and local gate
selection start from, with a hidden machine-readable marker. Only markers from
trusted authors count, and the latest one is authoritative. A child resumed
after human review, or resumed from `agent-working`, keeps the recorded child
base while it is an ancestor of the branch head, and its new comment carries it
forward. So the commits of its earlier child cycles, and any commits a human
made on the branch in the meantime, count as the child's work. If other
children were implemented after the recorded child base, the child base moves
to the verified head of the latest one, so the child's implementation evidence
and review session never claim their commits (see
[ADR 0001](docs/adr/0001-child-base-falls-back-when-another-child-lands-on-top.md)).
The start comment, the implementation evidence, and the review prompt then name
the commits of the child's earlier child cycles that this no longer covers,
taken up to the child base recorded by the first child implemented on top; if
that child has no recorded child base, they name every commit before the moved
child base. Local gates and required CI checks are still selected from every
path changed since the recorded child base, which the new start comment keeps
recording. If the recorded child base is no longer on the branch (it was
rebased or rewritten), Ralph starts from the current head and says so in the
comment. A child without a marker starts from the current head.

The loop of a standard run is:

1. Run `$implement` with the configured implementation model. The review
   requested inside that skill stays.
2. Run the applicable local gates when enabled. On failure, start a fresh
   local gate correction, then rerun the gates.
3. Push the branch and create or reuse the cumulative draft PR. Wait for CI on
   that exact commit when `ci_checks = true`. With `ci_checks = false`, publish
   after the local gates pass without querying CI. On a product CI failure, collect diagnostic evidence, start a
   fresh CI correction, rerun local gates, push, and wait for CI again.
4. Mark the child `implemented` only when the work is complete and verification
   passes, removing every other lifecycle label it carries. Continue with the
   next runnable child. The parent ultimately goes to human review; only a human
   merges.

The optional final number is the run budget: the total number of agent
sessions the run may start, shared by all children. The default is
`max(4, ready_children * 5)`, where `ready_children` counts the ready children
when the run starts, after the startup choice below: open, not `implemented`,
with `ready-for-agent` as their only triage label and only lifecycle label,
blocked or not. Children resumed at that choice count. Closed, implemented,
agent-working, human-review, and ready-for-human children do not count, so
resuming a parent with one child left gets 5 sessions.
The count is taken once; relabelling a child during the run does not change its
budget. It counts sessions, not tokens or time.

A standard run has no separate review session, including after corrections. The
`models.review` configuration entry is optional and unused by a standard run;
a reviewed run still requires it. A standard run
accepts `local_checks = true` or `false` and
`ci_checks = true` or `false`. Both settings are independent;
implementation evidence reports only the verification that was enabled.
The applicable gates still use the shared manifest's `before_agent_review`
selection flag, even though no separate review follows them.

An implementation or correction that returns `needs_correction` must still pass
its outstanding findings to an issue correction before publication. The
correction limit and the run budget remain in force. CI corrections must create
a new commit. Infrastructure retries do not consume the run budget.

Correction context is assembled by Python; it does not launch a separate LLM
or invoke the `handoff` skill. Prompts contain the parent and child issue URLs,
issue base and current commits, the previous session's short summary, and paths
to its complete result and outputs. Failure evidence includes:

- Local gates: the exact command, working directory, exit code, an excerpt from
  **each** output stream, and files containing the full stdout and stderr.
- CI: every currently failed CI check, its job URL, the repository check
  command, job/step metadata, and complete job logs fetched with
  [`gh run view --job … --log`](https://cli.github.com/manual/gh_run_view).
  Job metadata must match the failed commit and check before logs are used, and
  an empty log counts as unavailable. Older `gh` versions print an empty log,
  with success, when the run's log archive has no step logs; hence `gh` 2.70.0.
  If logs cannot be fetched, the prompt records that limitation, the retrieval
  error, and references for further diagnosis.

Run diagnostics are retained under `.ralph/diagnostics/<run-id>/`: `sessions/`
holds one directory per agent session (prompt, result, and outputs); `evidence/`
holds command metadata and complete output streams. Known credential patterns are
redacted before storage and handoff. Excerpts prioritize error details and stay
bounded; complete logs are referenced, not pasted into every prompt. Run
diagnostics are ignored by Git and are available to subsequent sessions in the
same checkout. Before writing anything else there, a run creates
`.ralph/.gitignore` containing `*` (an existing one is kept), so its diagnostics
never make the worktree dirty, even where the repository's `.gitignore` does not
list `.ralph/`.

A standard run's implementation evidence uses separate markers (still named
`ralph-experimental-*` for compatibility with earlier runs) and records the
verified head rather than claiming independent review. Each kind of run only
recovers its own interrupted publication markers: a child that is
`ready-for-agent` or `agent-working`, whose latest update is that run kind's
trusted implementation evidence with commits on the current branch, is marked
`implemented` without another session. Existing `implemented` labels
remain shared, so switching between standard and reviewed runs does not
revalidate children already implemented. Blocker checks accept trusted
implementation evidence from either kind of run when its commits are on the branch.

## Repository settings

- `repository` is the exact GitHub `owner/name` returned by `gh repo view`.
- `base_branch` is the protected branch Ralph must never modify directly.
- `remote` is the Git remote Ralph pushes to.
- `trusted_authors` is the non-empty allowlist of trusted authors for parent
  issues and children. Include the GitHub account Ralph runs as so a restarted
  run can trust its own implementation evidence.
- `labels` maps Ralph's eight label roles to labels in the target
  repository: the triage labels `needs_triage`, `needs_info`, `ready_for_agent`,
  `ready_for_human`, and `wontfix`, and the lifecycle labels `ready_for_agent`,
  `agent_working`, `human_review`, and `implemented`. `agent_working`
  (`agent-working` by default) marks the child a run is working on; only Ralph
  sets it. `init` and `check` offer to create missing ones (Yes / Later).
- `models` holds one model profile (engine, model, display name, and reasoning
  effort) each for implementation, independent review, and correction sessions.

The loader rejects missing and unknown settings, duplicate TOML keys, duplicate
authors, duplicate label values, unsafe repository paths, and unsupported mode
values before starting an agent or changing GitHub state.

## Codex and Claude Code

Each model profile accepts `engine = "codex"` or `engine = "claude"`.
Omitting `engine` defaults to Codex, preserving existing configurations. Choose
an engine separately for implementation, independent review, and corrections;
a standard run uses only the implementation and correction profiles.
Use a model name supported by the selected CLI and your account.

For example, to implement and correct with Claude while retaining the existing
Codex reviewer, change the implementation and correction profiles:

```toml
[models.implementation]
engine = "claude"
name = "sonnet"
display_name = "Claude"
reasoning_effort = "medium"

[models.correction]
engine = "claude"
name = "sonnet"
display_name = "Claude correction"
reasoning_effort = "medium"
```

Python 3.11+, Git, authenticated `gh` 2.70.0 or newer, and the authenticated CLI for each selected
engine must be available on `PATH`. Project test tools are still required.
Claude efforts are `low`, `medium`, `high`, `xhigh`, or `max`; the selected model
may support only a subset. Codex retains its existing effort options. Model and
account availability are checked by the respective CLI at session start.

Both kinds of run use `ralph_kit/agent.py` for engine-specific commands and result
normalization. Codex writes the schema result to a file; Claude runs in print
mode and returns it in `structured_output`. The same Ralph contract and Git
checks apply to both. Claude error envelopes or missing structured results stop
the run; session outputs remain in the run diagnostics with the engine and return code.

For Claude, Ralph invokes `/implement` directly and explicitly instructs the
agent to read `AGENTS.md`. Repo skills are exposed through individual relative
symlinks in `.claude/skills/`, pointing to `.agents/skills/`. Keep these links
available for `implement` and its dependencies. Each agent session starts
fresh.

The existing unattended permissions policy is retained: Codex uses
`--dangerously-bypass-approvals-and-sandbox`; Claude uses
`--dangerously-skip-permissions`. Both agents can execute commands and modify
files. Run in a checkout and environment appropriate for that access. Ralph
still owns publishing and a human owns merge.

CLI references: [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)
and [Claude Code programmatic usage](https://code.claude.com/docs/en/headless).

## Verification

`verification.local_checks` (local gates) and `verification.ci_checks` (CI
checks) are independent booleans:

| `local_checks` | `ci_checks` | Behaviour |
| --- | --- | --- |
| `true` | `true` | Run applicable local gates, then require exact-SHA CI. |
| `true` | `false` | Run applicable local gates and do not query CI. |
| `false` | `true` | Skip local gates and require exact-SHA CI. |
| `false` | `false` | Skip verification; implementation evidence explicitly records that it was disabled. A reviewed run still performs independent agent review. |

Use TOML booleans without quotes. These settings replace the former `mode` and
`local_gates` configuration keys; update custom configurations before running.

The last combination is an explicit low-assurance mode for repositories with no
CI and no local test commands. Ralph still pushes a draft pull request, verifies
that the completed commit is the published one, and leaves merge authority to a
human.

The quality policy (quality module and manifest) is required whenever local
gates or CI checks are enabled. It is not read when both are disabled.

Both kinds of run retain redacted session outputs and local-gate or CI command
evidence in the run diagnostics under `.ralph/diagnostics/<run-id>/`. Each new
agent session receives the issue and commit context plus paths to the previous
result and full command outputs. Failed CI jobs are matched to the checked commit
before their logs are passed to a correction session. A reviewed run also passes
this evidence to the reviewer.

## Incomplete issue work

Each agent session reports an `outcome`, a short `summary`, and a `findings` array:

- `committed`: the requested work is complete and new commits exist.
- `no_changes`: the requested work is complete without new commits.
- `needs_correction`: concrete implementation or review gaps remain. This may
  accompany committed partial work; `findings` must list every remaining gap and
  its issue requirement.
- `blocked`: a prerequisite or human decision prevents further work.
- `failed`: execution cannot continue safely.

Successful results must have an empty `findings` array. Completed changes and
passed checks belong in `summary`; `findings` contains unresolved actionable
problems only. All agent session prompts, including local-gate corrections, explain
this distinction. A local-gate correction reports completeness of that gate;
the independent issue review separately checks the entire issue.

Findings are retained separately from the summary, so shortening a long summary
does not discard correction instructions. Ralph redacts credentials from findings
before passing them to correction sessions or publishing them on GitHub.

When implementation, independent review, or a CI correction returns
`needs_correction`, Ralph starts a fresh issue correction, preserves the existing commits, and requires
a new commit. It then runs the configured local gates and an independent review
against every issue acceptance criterion, including pre-existing partial work.
CI corrections also receive an independent review before the new commit is
published and checked.

The **correction limit** allows each child at most **three correction
sessions**, whatever their kind: issue corrections, local gate corrections, and
CI corrections share one count, and each prompt shows it (`Correction attempt
2/3`). Every correction also consumes the run budget. An exhausted correction
limit or run budget, issue or CI corrections without a new commit,
and `blocked` or `failed` results stop the run and leave the child open with
`human-review` as its only lifecycle label. The handoff comment includes the
outstanding findings and, for local gate or CI failures, the last diagnostic.
Every handoff comment for a child whose child cycle started also says that the
child's commits stay on the branch and will count as its work if it is
relabelled `ready-for-agent`.

Ralph publishes implementation evidence and adds `implemented` only after the
issue review and configured verification succeed for the final commit. A child
already marked `human-review` is not resumed automatically; remove that label
and restore `ready-for-agent` when it is ready for another run.

A run that ends without releasing its child (a crash, an error stop, or a killed
process) leaves it `agent-working`. At startup, before any agent session, a run
first marks `implemented` every child whose implementation evidence was already
published, without asking. If children of the parent are still `agent-working`
after that, Ralph shows one warning naming them all: it cannot tell an
interrupted run from a run still active elsewhere, and resuming while another run
is active makes two agents do the same work on two branches. A resumed child keeps
its recorded child base, as described above, so commits already made for it
count as its work. One choice applies to all of them:

- **Stop** (the default) ends the run as `stopped safely` before any session,
  leaving the children `agent-working`. The terminal summary lists each one as
  "agent working from an earlier or concurrent run", with the next action: if no
  other run is working on this parent, inspect the branch, relabel the child
  `ready-for-agent`, then rerun.
- **Resume** relabels each of them `ready-for-agent` and the run continues; they
  count in the default run budget.

The choice uses the arrow keys and needs a terminal on both stdin and stdout.
Without one (`nohup`, a scheduler, redirected output), Ralph takes the Stop path
and puts the warning in the terminal summary. Ctrl-C at the prompt counts as
Stop.

A run always releases its own child before it selects the next one. So if,
while selecting its next child, a run finds a remaining child `agent-working`,
that child belongs to another run started on the same parent. The run stops
safely, without a prompt, before any further agent session, and leaves that
child as it is, even if its implementation evidence is already published. The
stop reason and the terminal summary name the child, listed as "agent working
in another run on this parent", so you can find and stop the run that should not
be running. This guard is best-effort, not a lock: there is no run marker, no run
identifier, and no read-after-write check. Two runs that read the labels before
either writes `agent-working` can still both take the same child, and a run
started on the same parent is only noticed at startup or at its next child
selection.

Ctrl-C (SIGINT), `kill` (SIGTERM), and closing the terminal (SIGHUP) all
interrupt a run the same way. Ralph ends the agent session in flight,
terminating its engine process, and releases the child it was working on. If the
branch head still equals the commit its child cycle started from, nothing was
committed for it in that child cycle:
the child goes back to `ready-for-agent` and the next run starts it again.
Otherwise the child goes to `human-review` with a handoff comment naming the
unverified commit range `<base>..<head>`; inspect those commits, then close the
child or relabel it `ready-for-agent` and rerun. A child whose implementation
evidence was already published is marked `implemented` instead. Ralph then
publishes the terminal summary with the outcome `interrupted` on the parent (and
the draft PR, if one exists), listing that child with its reason and next
action, prints it, and exits with the shell's code for that signal: 130, 143,
or 129. An interruption between two children releases nothing and publishes the
same summary. A second interruption during this cleanup exits immediately,
leaving the labels as they are, so the next run asks about a child still
`agent-working` as described above. `kill -9`, crashes, and power loss are never
caught: they release nothing and publish no interrupted summary. A signal
ignored at launch, such as SIGHUP under `nohup`, stays ignored.

## Readiness check

Before launching, from the target repository root on each machine (including a VPS):

```sh
ralph-kit check
# For a reviewed run:
ralph-kit check --review
```

Commit the prepared files and use a dedicated branch first. The readiness check validates
TOML and placeholders, Python and required CLI availability (and the minimum `gh` version), Git branch/remote
and clean worktree, `.ralph/` ignored by Git (any ignore source counts; the check
never edits `.gitignore`), GitHub access/repository/trusted account/write permission/labels,
the packaged result schema, the implement skill for each engine, the flags Ralph passes
to each configured engine (listed in the engine's own help, so an update that drops one
blocks before a run), and the enabled quality policy.
It inspects local gate commands and CI check names without executing them.
Disabled verification may keep placeholder entries; when both are disabled the
quality policy is not loaded.
Results are grouped by section with what was found; each failure shows the fix
(often a command to run), and checks that depend on a missing tool are reported
as not verified instead of failing. Colors are dropped when the output is not a
terminal (or with `NO_COLOR`).
A nonzero exit code means a blocking check failed. In a terminal, missing labels
are offered for creation after the report (Yes / Later); that is the only GitHub
write. Engines only print their help; no agent session, project check, CI run, push,
issue or PR mutation occurs. The configured
quality module is imported: it must be trusted and have no import-time side effects.

A successful readiness check does not validate agent authentication/model access,
actual skill discovery or transitive dependencies, Git push credentials, project
dependencies and services, test results, actual CI workflow names, or the readiness
of the parent issue, its children and their blockers. Those remain the user's
responsibility, along with accepting the agents' permission-bypass options and
reviewing the resulting PR before merge.
