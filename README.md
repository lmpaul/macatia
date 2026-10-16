# ralph-kit

Ralph runs coding agents (Codex or Claude Code) through a parent GitHub issue,
one runnable sub-issue at a time: implement, verify, correct, publish a draft PR.
A human reviews and merges.

`ralph-kit` is installed once per machine and run from any repository. Each
repository only keeps its own `ralph.toml` (plus, when checks are enabled, its
quality module and manifest).

## Install

Requires Python 3.11+ (uv can provide it), Git, an authenticated `gh`, and the
CLI of each configured agent engine.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh        # once per machine
uv tool install git+https://github.com/lmpaul/ralph-kit # installs `ralph-kit` on PATH
uv tool upgrade ralph-kit                               # pick up new versions
```

Pin a release with `git+https://github.com/lmpaul/ralph-kit@v0.1.0`.

## Use in a repository

From the repository root:

```sh
ralph-kit init                 # writes ./ralph.toml (detects what it can, asks the rest)
ralph-kit check                # read-only readiness check
ralph-kit run 123              # implement the sub-issues of parent issue #123
ralph-kit run 123 10           # same, with at most 10 agent sessions
ralph-kit run --review 123     # adds an independent review session per child
```

Commit `ralph.toml` and run on a clean, dedicated branch (never the base branch).

`init` reads the GitHub repository, default branch, remote and current `gh`
account, and enables local and CI checks when `.github/scripts/quality.py` and
`.github/quality-gates.json` exist. It asks only for what it could not detect,
plus the agent engine, models and reasoning efforts. Only installed agent CLIs
are offered. Codex models and their supported efforts come from your account's
catalog (`codex debug models`, with a free-text fallback); Claude Code has no
model listing, so `init` offers its documented aliases (`fable`, `opus`,
`sonnet`, `haiku`), which always follow the latest version. Without a terminal
(agents, CI) it asks nothing and leaves placeholders that `ralph-kit check` reports.
`--config path/to/other.toml` selects another configuration. Ralph writes run
evidence to `.ralph/`; add `.ralph/` to the repository's `.gitignore`.

## Develop ralph-kit

```sh
git clone git@github.com:lmpaul/ralph-kit.git && cd ralph-kit
uv tool install -e .                          # `ralph-kit` now runs this checkout
python3 -m unittest discover -s tests         # offline tests (~5 min)
```

Tests use temporary Git repositories and fake GitHub/Codex/Claude commands; they
do not launch real agents or mutate remote issues.

## Runners: `run` and `run --review`

`ralph-kit run` is the implementation-only loop. `ralph-kit run --review` is the
original loop with an independent review session. The sequence of `run` is:

1. Run `$implement` with the configured implementation model. The review
   requested inside that skill stays.
2. Run the applicable local gates when enabled. On failure, start a fresh
   correction session, then rerun the gates.
3. Push the branch and create or reuse the cumulative draft PR. Wait for CI on
   that exact commit when `ci_checks = true`. With `ci_checks = false`, publish
   after the local gates pass without querying CI. On a product CI failure, collect diagnostic evidence, start a
   fresh correction, rerun local gates, push, and wait for CI again.
4. Mark the child `implemented` only when the work is complete and checks pass.
   Continue with the next runnable child. The parent ultimately goes to human
   review; only a human merges.

The optional final number limits the total agent sessions. The default remains
`max(4, number_of_children * 5)`. This is a session budget, not a token budget.

There is no separate review session, including after corrections. The existing
`models.review` configuration entry is optional and unused by `run`;
the original runner with independent review still requires it. This experiment
accepts `local_checks = true` or `false` and
`ci_checks = true` or `false`. Both settings are independent;
success evidence reports only the checks that were enabled.
The applicable gates still use the shared manifest's `before_agent_review`
selection flag, even though no separate review follows them.

An implementation or correction that returns `needs_correction` must still pass
its outstanding findings to another correction session before publication. The
two-attempt issue-correction limit and the global session budget remain in
force. CI corrections must create a new commit. Infrastructure retries do not
consume agent sessions.

Correction context is assembled by Python; it does not launch a separate LLM
or invoke the `handoff` skill. Prompts contain the parent and child issue URLs,
issue base and current commits, the previous session's short summary, and paths
to its complete result and outputs. Failure evidence includes:

- Local gates: the exact command, working directory, exit code, an excerpt from
  **each** output stream, and files containing the full stdout and stderr.
- CI: every currently failed required check, its job URL, the repository check
  command, job/step metadata, and complete job logs fetched with
  [`gh run view --job … --log`](https://cli.github.com/manual/gh_run_view).
  Job metadata must match the failed commit and check before logs are used.
  If logs cannot be fetched, the prompt records that limitation, the retrieval
  error, and references for further diagnosis.

Evidence is retained under `.ralph/diagnostics/<run-id>/`: `iterations/` holds
session prompts, results, and outputs; `evidence/` holds command metadata and
complete output streams. Known credential patterns are redacted before storage
and handoff. Excerpts prioritize error details and stay bounded; complete logs
are referenced, not pasted into every prompt. These artifacts are ignored by
Git and are available to subsequent sessions in the same checkout.

Success comments use separate experimental markers and `verified_head` rather
than claiming independent review. Each loop only recovers its own interrupted
publication markers. Existing `implemented` labels remain shared, so switching
scripts does not revalidate children already marked complete. Dependency checks
accept trusted evidence from either version when its commits are on the branch.

## Repository settings

- `repository` is the exact GitHub `owner/name` returned by `gh repo view`.
- `base_branch` is the protected branch Ralph must never modify directly.
- `remote` is the Git remote Ralph pushes to.
- `trusted_authors` is the non-empty allowlist for parent and child issues.
  Include the GitHub account used by the loop so a restarted run can trust its
  own evidence comments.
- `labels` maps Ralph's seven canonical roles to labels that already exist in
  the target repository.
- `models` selects the CLI engine, model, display name, and reasoning effort for
  implementation, independent review, and correction sessions.

The loader rejects missing and unknown settings, duplicate TOML keys, duplicate
authors, duplicate label values, unsafe repository paths, and unsupported mode
values before starting an agent or changing GitHub state.

## Codex and Claude Code

Each model profile accepts `engine = "codex"` or `engine = "claude"`.
Omitting `engine` defaults to Codex, preserving existing configurations. Choose
an engine separately for implementation, independent review, and corrections;
`run` uses only the implementation and correction profiles.
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

Both loops use `ralph_kit/agent.py` for engine-specific commands and result
normalization. Codex writes the schema result to a file; Claude runs in print
mode and returns it in `structured_output`. The same Ralph contract and Git
checks apply to both. Claude error envelopes or missing structured results stop
the run; session outputs remain in diagnostics with the engine and return code.

For Claude, Ralph invokes `/implement` directly and explicitly instructs the
agent to read `AGENTS.md`. Repo skills are exposed through individual relative
symlinks in `.claude/skills/`, pointing to `.agents/skills/`. Keep these links
available for `implement` and its dependencies. Each invocation starts a fresh
session.

The existing unattended permissions policy is retained: Codex uses
`--dangerously-bypass-approvals-and-sandbox`; Claude uses
`--dangerously-skip-permissions`. Both agents can execute commands and modify
files. Run in a checkout and environment appropriate for that access. Ralph
still owns publishing and a human owns merge.

CLI references: [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)
and [Claude Code programmatic usage](https://code.claude.com/docs/en/headless).

## Verification checks

`verification.local_checks` and `verification.ci_checks` are independent booleans:

| `local_checks` | `ci_checks` | Behaviour |
| --- | --- | --- |
| `true` | `true` | Run applicable local gates, then require exact-SHA CI. |
| `true` | `false` | Run applicable local gates and do not query CI. |
| `false` | `true` | Skip local gates and require exact-SHA CI. |
| `false` | `false` | Skip automated verification; issue evidence explicitly records that it was disabled. The original runner still performs independent agent review. |

Use TOML booleans without quotes. These settings replace the former `mode` and
`local_gates` configuration keys; update custom configurations before running.

The last combination is an explicit low-assurance mode for repositories with no
CI and no local test commands. Ralph still pushes a draft pull request, verifies
that the completed SHA is the published SHA, and leaves merge authority to a
human.

The quality module and manifest are required whenever CI or local gates are
enabled. They are not read when both are disabled.

Both loops retain redacted session outputs and local-gate or CI command evidence
under `.ralph/diagnostics/<run-id>/`. Each new session receives the issue and
commit context plus paths to the previous result and full command outputs.
Failed CI jobs are matched to the checked commit before their logs are passed
to a correction session. The loop with review also passes this evidence to the reviewer.

## Incomplete issue work

Agent results contain `outcome`, a short `summary`, and a `findings` array:

- `committed`: the requested work is complete and new commits exist.
- `no_changes`: the requested work is complete without new commits.
- `needs_correction`: concrete implementation or review gaps remain. This may
  accompany committed partial work; `findings` must list every remaining gap and
  its issue requirement.
- `blocked`: a prerequisite or human decision prevents further work.
- `failed`: execution cannot continue safely.

Successful results must have an empty `findings` array. Completed changes and
passed checks belong in `summary`; `findings` contains unresolved actionable
problems only. All session prompts, including local-gate corrections, explain
this distinction. A local-gate correction reports completeness of that gate;
the independent issue review separately checks the entire issue.

Findings are retained separately from the summary, so shortening a long summary
does not discard correction instructions. Ralph redacts credentials from findings
before passing them to correction sessions or publishing them on GitHub.

When implementation or independent review returns `needs_correction`, Ralph
starts a fresh correction session, preserves the existing commits, and requires
a new commit. It then runs the configured local gates and an independent review
against every issue acceptance criterion, including pre-existing partial work.
CI corrections also receive an independent review before the new commit is
published and checked.

Each child allows at most **two issue-correction sessions**, shared across
implementation, review, and CI. These sessions also consume the existing global
agent budget. Exhausted retries or budget, corrections without a new commit,
and `blocked` or `failed` results stop the run and leave the child open with
`human-review`. Outstanding findings are included in the handoff.

Ralph publishes implementation evidence and adds `implemented` only after the
issue review and configured verification succeed for the final commit. An issue
already marked `human-review` is not resumed automatically; remove that label
and restore `ready-for-agent` when it is ready for another run.

## Readiness check

Before launching, from the target repository root on each machine (including a VPS):

```sh
ralph-kit check
# For the runner with independent review:
ralph-kit check --review
```

Commit the prepared files and use a dedicated branch first. The check validates
TOML and placeholders, Python and required CLI availability, Git branch/remote
and clean worktree, GitHub access/repository/trusted account/write permission/labels,
the packaged result schema, the implement skill for each engine, and the enabled quality policy.
It inspects local commands and CI names without executing them. Disabled checks
may keep placeholder entries; when both are disabled the policy is not loaded.
A nonzero exit code means a blocking check failed. GitHub requests are read-only;
no agent, project check, CI run, push, issue or PR mutation occurs. The configured
quality module is imported: it must be trusted and have no import-time side effects.

A successful check does not validate agent authentication/model access, actual skill
discovery or transitive dependencies, Git push credentials, project dependencies
and services, test results, actual CI workflow names, or issue/sub-issue/dependency
readiness. Those remain the user's responsibility, along with accepting the
runner's permission-bypass options and reviewing the resulting PR before merge.
