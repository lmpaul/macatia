# ralph-kit

Ralph runs a loop of coding agents (Codex or Claude Code) over a parent GitHub
issue, one runnable child (a GitHub sub-issue) at a time: implement, verify,
correct, publish a draft PR. A human reviews and merges. The project vocabulary is
defined in [GLOSSARY.md](GLOSSARY.md).

`ralph-kit` is installed once per machine and run from any repository. Each
repository only keeps its own `ralph.toml` (plus, when verification is enabled,
its quality module and manifest).

## Install

Requires Python 3.11+ (uv can provide it), Git, an authenticated `gh`, and the
CLI of each configured agent engine.

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
```

Commit `ralph.toml` and run on a clean, dedicated branch (never the base branch).

`init` first requires `git`, an authenticated `gh` and at least one agent CLI
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
diagnostics to `.ralph/`; add `.ralph/` to the repository's `.gitignore`.

## Develop ralph-kit

```sh
git clone git@github.com:lmpaul/ralph-kit.git && cd ralph-kit
uv tool install -e .                          # `ralph-kit` now runs this checkout
uv run python -m unittest discover -s tests   # offline tests (~1.5 min), with dependencies
```

Tests use temporary Git repositories and fake GitHub/Codex/Claude commands; they
do not launch real agents or mutate remote issues. Run-level tests in
`tests/test_run.py` start a whole run in memory through injected run adapters
(`tests/run_support.py`: a GitHub fake and scripted sessions over real Git).

## The loop: `run` and `run --review`

Each run executes the loop over the parent issue's runnable children.
`ralph-kit run` starts a standard run, with no review session.
`ralph-kit run --review` starts a reviewed run: the same loop, plus a review
session for each child once its local gates pass, repeated after each issue
correction that review causes. The loop of a standard run is:

1. Run `$implement` with the configured implementation model. The review
   requested inside that skill stays.
2. Run the applicable local gates when enabled. On failure, start a fresh
   local gate correction, then rerun the gates.
3. Push the branch and create or reuse the cumulative draft PR. Wait for CI on
   that exact commit when `ci_checks = true`. With `ci_checks = false`, publish
   after the local gates pass without querying CI. On a product CI failure, collect diagnostic evidence, start a
   fresh CI correction, rerun local gates, push, and wait for CI again.
4. Mark the child `implemented` only when the work is complete and verification
   passes. Continue with the next runnable child. The parent ultimately goes to
   human review; only a human merges.

The optional final number is the run budget: the total number of agent
sessions the run may start, shared by all children. The default remains
`max(4, number_of_children * 5)`. It counts sessions, not tokens or time.

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
  Job metadata must match the failed commit and check before logs are used.
  If logs cannot be fetched, the prompt records that limitation, the retrieval
  error, and references for further diagnosis.

Run diagnostics are retained under `.ralph/diagnostics/<run-id>/`: `sessions/`
holds one directory per agent session (prompt, result, and outputs); `evidence/`
holds command metadata and complete output streams. Known credential patterns are
redacted before storage and handoff. Excerpts prioritize error details and stay
bounded; complete logs are referenced, not pasted into every prompt. Run
diagnostics are ignored by Git and are available to subsequent sessions in the
same checkout.

A standard run's implementation evidence uses separate markers (still named
`ralph-experimental-*` for compatibility with earlier runs) and records the
verified head rather than claiming independent review. Each kind of run only
recovers its own interrupted publication markers. Existing `implemented` labels
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
- `labels` maps Ralph's seven label roles to labels in the target
  repository. `init` and `check` offer to create missing ones (Yes / Later).
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

Python 3.11+, Git, authenticated `gh`, and the authenticated CLI for each selected
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
`human-review`. The handoff comment includes the outstanding findings and, for
local gate or CI failures, the last diagnostic.

Ralph publishes implementation evidence and adds `implemented` only after the
issue review and configured verification succeed for the final commit. A child
already marked `human-review` is not resumed automatically; remove that label
and restore `ready-for-agent` when it is ready for another run.

## Readiness check

Before launching, from the target repository root on each machine (including a VPS):

```sh
ralph-kit check
# For a reviewed run:
ralph-kit check --review
```

Commit the prepared files and use a dedicated branch first. The readiness check validates
TOML and placeholders, Python and required CLI availability, Git branch/remote
and clean worktree, GitHub access/repository/trusted account/write permission/labels,
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
