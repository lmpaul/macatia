# ralph-kit

Ralph runs a loop of coding agents (Codex or Claude Code) over a parent GitHub
issue, one runnable child (a GitHub sub-issue) at a time, or over a standalone
issue on its own: implement, verify, correct, publish a draft PR. A human
reviews and merges. The project vocabulary is defined in
[GLOSSARY.md](GLOSSARY.md).

`ralph-kit` is installed once per machine and run from any repository. Each
repository only keeps its own `ralph.toml` (plus, when verification is enabled,
its quality module and manifest).

## Install

Requires Python 3.11+ (uv can provide it), Git, an authenticated `gh` 2.70.0 or
newer, and the CLI of each configured agent engine.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh        # once per machine
uv tool install git+https://github.com/lmpaul/ralph-kit # installs `ralph-kit` on PATH
uv tool upgrade ralph-kit --reinstall                   # pick up the latest commit
```

Pin a release with `git+https://github.com/lmpaul/ralph-kit@v0.6.3`.

## Use in a repository

From the repository root:

```sh
ralph-kit init                 # writes ./ralph.toml (detects what it can, asks the rest)
ralph-kit init --config b.toml  # writes ./b.toml instead, for a second configuration
ralph-kit configure            # change sections of ./ralph.toml from a menu
ralph-kit update-config        # add the defaulted settings ./ralph.toml leaves out, then their GitHub labels
ralph-kit check                # readiness check (offers to create or recolour labels)
ralph-kit check --review       # also checks the review role (implied by review_by_default = true)
ralph-kit run                  # at a terminal, pick among the startable run targets
ralph-kit run 123              # standard run: implement the children of parent issue #123
ralph-kit run --review 123     # reviewed run: adds an independent review session per child
ralph-kit run --no-review 123  # standard run, even when review_by_default = true
ralph-kit run --caffeinate 123 # keep the machine awake until the run ends
ralph-kit run --no-caffeinate 123 # let it sleep, even when caffeinate_by_default = true
ralph-kit run --tmux 123       # offer to restart the run in a persistent terminal (tmux)
ralph-kit close                # at a terminal, pick among the closable run targets
ralph-kit close 123            # closure: close #123's children, then #123, or standalone #123 alone (asks first)
ralph-kit close --yes 123      # same, without the confirmation
ralph-kit close --force 123    # close despite failed checks, after typing 123
ralph-kit reset 123            # reset: make #123 and its children ready for an agent again (asks first)
ralph-kit reset --yes 123      # same, without the confirmation
```

`--caffeinate` holds a sleep lock for the run, from once the configuration is
loaded until the run ends: `caffeinate -i` on macOS,
`systemd-inhibit --what=idle:sleep` on Linux, `SetThreadExecutionState` on Windows.
Where none is available, the run warns (`No sleep lock: ...`) and continues.
The display may still turn off.

With `caffeinate_by_default = true` in `ralph.toml`, every run holds the sleep
lock without `--caffeinate`, and says so once at startup;
`ralph-kit run --no-caffeinate` runs without it instead. `--no-caffeinate` is
also accepted when the setting is off, and `--caffeinate` holds the sleep lock
whatever the setting, without the notice. `--caffeinate` together with
`--no-caffeinate` is a usage error.
With the setting on, `ralph-kit check` also reports whether this machine can
provide the sleep lock, as a warning only: a run continues without one.

`--tmux` offers to restart the run in a persistent terminal: a tmux session
named `ralph-kit-<repository name>-<run target>`, where the repository name is
the part after `owner/` in the configured `repository`, with `.` and `:` replaced
by `_`. Once the run target passes its startup checks, and before the run branch
offer, Ralph asks "Where should this run continue?":

- **Restart in ralph-kit-…** (the default) releases the sleep lock, then
  restarts the run in a new persistent terminal started in the repository root,
  and attaches this terminal to it. The restarted run has the same `--review`,
  `--caffeinate` and `--config` flags, never `--tmux`, and the run target, the
  one picked when the run started without one. When the run ends, the
  persistent terminal stays open on your shell. Detach with `Ctrl-b d`; reattach with
  `tmux attach -t ralph-kit-…`.
- **Continue here** runs in this terminal, as without `--tmux`.
- **Stop**, or Ctrl-C at the prompt, ends the run with nothing changed.

When a tmux session with exactly that name already exists, the run stops
before the question, with nothing changed:
`ralph-kit-ralph-kit-129 already exists: attach to it with tmux attach -t ralph-kit-ralph-kit-129, or end it first`.
A session of another repository or run target does not stop the run.

Without ISSUE, the run target is picked first, outside tmux. Without
`--tmux`, nothing is asked.

A `--tmux` run that cannot use a persistent terminal stops at once, before
reading the configuration or GitHub and without taking the sleep lock:

- `tmux not found: install tmux, or run without --tmux`
- `already inside tmux: run without --tmux`
- `--tmux needs a terminal: run in a terminal, or without --tmux`, when stdin
  or stdout is not a terminal.

`ralph-kit check` does not require tmux.

Commit `ralph.toml` (and `.gitignore` when `init` changed it) and run on a clean worktree.

A run started on the base branch offers a run branch named
`ralph-kit/issue-<parent>` once every other startup check has passed, the parent
included. Ralph first looks for that branch locally and on the remote (with
`git ls-remote`, which writes no local ref; an unreachable remote stops the run)
and warns that it is on the base branch.

If the branch exists neither locally nor on the remote, Ralph asks "Create
ralph-kit/issue-N from an up-to-date <base>?":

- **Create** (the default) pulls the base branch fast-forward only from the
  remote, then creates and switches to the run branch. A base branch with commits
  that are not on the remote stops the run with no branch created.
- **Stop**, or Ctrl-C at the prompt, ends the run with nothing changed.

If the branch exists locally, on the remote, or both, Ralph asks "Switch to the
existing ralph-kit/issue-N?":

- **Switch** (the default) moves to the branch and follows the remote one. A
  branch only on the remote is fetched and checked out tracking it. A local
  branch behind the remote one is fast-forwarded. A local branch that has
  diverged from the remote one stops the run, leaving you on the branch with
  nothing rewritten to reconcile it yourself. The base branch is not pulled, and
  nothing is merged or rebased into the run branch.
- **Stop**, or Ctrl-C at the prompt, ends the run with nothing changed.

Once on the run branch, Ralph pushes it with its upstream before any agent
session unless it is already on the remote at the same commit with that upstream.
A failed push stops the run and leaves you on the local branch. The draft PR is
still opened at the first publication.

Without a terminal the run stops with nothing changed and names the branch to
create or switch to. A detached HEAD is refused.
`ralph-kit check` passes on the base branch with a note that the run will offer
a `ralph-kit/issue-<parent>` branch, and still blocks on a detached HEAD.

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
After the model profiles, `init` asks for a backup (see
[Engine failures and the backup](#engine-failures-and-the-backup)) among the installed
agent CLIs, plus "No backup". It recommends and preselects the other installed CLI,
since an engine failure is rarely limited to one model, and preselects "No backup"
when only one CLI is installed. A chosen backup gets the same model and reasoning
effort questions, and must differ from the other profiles: a reasoning effort that
would make it identical to a session role's model profile is shown greyed out with
the reason, a model only when all its efforts would, and a model name typed by hand
is checked right after you enter it.
`--config path/to/other.toml` selects another configuration, relative to the
repository root: `init` writes it (its directory must exist, and `./ralph.toml` is left
untouched), and `run` and `check` read it. `init` refuses an existing file unless
`--force` is given, which overwrites only that file; its final message suggests
`ralph-kit check --config path/to/other.toml`. Ralph writes run
diagnostics to `.ralph/`. Unless Git already ignores it, `init` appends `.ralph/` to the
repository's `.gitignore` (creating the file if needed) and says so. Commit it with `ralph.toml`;
`check` reports `.ralph/` when Git does not ignore it.

`init` on an existing file points to `configure`: `ralph-kit configure [--config PATH]`
opens an existing, valid configuration and shows a menu of its sections (model
profiles, backup, trusted authors, verification, Git, reviewed runs by default, keep the
machine awake by default, child session limits), each with its current value
and `*` once changed. Pick a section to change only what it holds; nothing is
re-detected over an existing value, and the values no section edits (label names,
quality policy paths, display names, a correction profile distinct from
implementation) are kept. Model profiles picks a session role (implementation, review or
correction) and asks its engine (the installed agent CLIs plus the role's current one,
which is the default even when its CLI is not installed here), model and reasoning effort
as `init` does, with the current values as defaults; a model keeps its display name unless
it changes. When implementation changes and correction was identical to it, `configure`
asks whether correction follows (Yes by default). With a backup, efforts that would make
the profile identical to it, and models whose every effort would, are shown unavailable
("same as backup"); a free model name clashing with it is explained and asked again.
Backup adds one when there is none, otherwise changes or removes it (removing drops
`[models.backup]` from the file). Its engine is asked as in `init`, an installed CLI that
no session role uses first, with the current backup's engine kept available even when its
CLI is not installed here (every engine is offered when no agent CLI is); then its model
and reasoning effort, with the current values as defaults. Efforts that would make the backup identical to a session role's model
profile, and models whose every effort would, are shown unavailable with the reason
("same as review"); a free model name clashing with one is explained and asked again.
Trusted authors lists the configured accounts, adds one
(a duplicate is not added twice) or removes one, but never the last, and warns when
the current `gh` account is not trusted, since Ralph would then ignore its own
implementation evidence. Git changes the repository (owner/name), base branch and
remote, each asked with its current value as the default and the detected one (gh
repository, default branch, Git remotes) shown when it differs; without `gh` or `git`
output, editing still works. After a repository change, the labels missing there are
offered for creation (Yes / Later), as `init` does, and created once the file is
written. Verification turns local gates and
CI checks on or off independently; enabling one without the quality module and
manifest is accepted with a warning that `ralph-kit check` fails until they exist.
Reviewed runs by default turns `review_by_default` on or off. Turning it on without a
review profile first asks for one, as Model profiles does for review; turning it off keeps
the review profile.
Keep the machine awake by default turns `caffeinate_by_default` on or off; it asks
for nothing else.
Child session limits changes `standard` or `reviewed` in `[child_session_limit]`, with
the current value as the default and the same validation as loading (an integer, at
least 1 for standard and 2 for reviewed). `init` writes the table with its defaults.
Save lists each changed key as `key: old → new`, writes the file, validates it and
suggests committing it, then `ralph-kit check`; Save without changes, Quit without
saving and Ctrl-C write nothing. The file is rewritten in the layout `init` uses, so
comments added by hand are dropped (`configure` says so when it opens the file). When it
opens a file with defaulted settings (settings the file leaves out, which take their
default value), it lists them and points to `ralph-kit update-config`, which writes them;
Save does not write them unless a value changes. It needs a terminal; without one, edit
the TOML directly. An invalid configuration is refused with the error: fix the file by
hand (a missing label key names the line to add); `ralph-kit init --force` would start
over and lose its settings.
Unknown keys usually mean the file was written by a newer ralph-kit (for example
committed from another machine): the error suggests `uv tool upgrade ralph-kit --reinstall`.

A newer ralph-kit can add a setting. `ralph-kit check` lists the defaulted
settings the file leaves out other than label roles (`review_by_default`,
`caffeinate_by_default`, each `child_session_limit` key) as information that
does not fail the check, and warns about defaulted label roles.
`ralph-kit update-config [--config PATH]` writes each defaulted setting the file
leaves out, with its default value and the comment `init` writes next to it: `review_by_default` and `caffeinate_by_default`
after the last top-level setting, before the first table, each label role at
the end of `[labels]` with its default name, and a `[child_session_limit]` table
with `standard` and `reviewed` at the end of the file, or the limit a partial
table leaves out at the end of that table, without the table's comment. A comment
block directly above the next table stays with that table. The rest of the file and its comments
stay as they are; `[models.review]` and `[models.backup]` are never added. It lists
each added setting as `key = value`, and with nothing to add says so and writes
nothing. It refuses when the default name is already another role's label, when
the labels are not in a `[labels]` table, or when a partial `child_session_limit`
is written as an inline table or with dotted keys, and then writes nothing. It validates the
completed file before writing it and, like `check`, offers to create the labels
missing on GitHub (Yes / Later; without a terminal it only prints the command). Commit the file afterwards: a run needs a
clean working tree.

For a variant of an existing configuration, copy the file, then change the copy:

```sh
cp ralph.toml review.toml
ralph-kit configure --config review.toml
ralph-kit check --config review.toml
```

## Develop ralph-kit

```sh
git clone git@github.com:lmpaul/ralph-kit.git && cd ralph-kit
uv tool install -e .                          # `ralph-kit` now runs this checkout
uv run python -m unittest discover -s tests   # offline tests (~35 s), with dependencies
```

Bump `__version__` in `src/ralph_kit/__init__.py` with every change to `ralph.toml`'s
keys, so `ralph-kit --version` tells whether a machine can read a newer file.

The default suite is offline: it never starts `ralph-kit run` as a subprocess,
launches a real agent, or calls GitHub. Each rule is tested at one level:

- **Child cycle rules** (correction limit, review session order, handoff on
  missing commits, child session limit checks inside a cycle):
  `tests/test_child_cycle.py`,
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
  covers `ralph-kit init` the same way, `tests/test_configure.py` covers
  `ralph-kit configure`, and `tests/test_update_config.py` covers
  `ralph-kit update-config`.
- **Closure** (every check, the confirmation, the writes and resuming
  them): `tests/test_close.py`, the close command's entry point in memory with
  the GitHub fake and a scripted confirmer.
- **Reset** (every refusal, the plan, the confirmation, the cumulative draft PR,
  the run branch, the labels, reset markers and recap, and resuming them): `tests/test_reset.py`, the reset command's entry
  point with the GitHub fake, a scripted confirmer and a temporary checkout with
  a bare `origin`.
- **Live suite**: the only proof of compatibility with the real `gh`, `codex`,
  and `claude`: `tests/test_live.py`, skipped unless `RALPH_LIVE=1` (see below).

### Live suite

Run it before a release and after upgrading `gh`, `codex` or `claude`. It needs
an authenticated `gh` and engine CLIs, and spends a few tokens, so it is not part
of the default suite, local gates or CI:

```sh
RALPH_LIVE=1 uv run python -m unittest discover -s tests -p test_live.py -v
```

It makes read-only `gh` calls of each shape a run or `close` parses (repository,
children, blockers, issue details, open pull requests, check runs, the Actions job,
job log and check run behind a check, the compare of the default branch with
its head, and the merged pull requests of a merged pull request's head commit)
through Ralph's GitHub adapter, against the
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

Each run executes the loop over the runnable children of its run target: a
parent issue's sub-issues, or a standalone issue itself.
`ralph-kit run` starts a standard run, with no review session.
`ralph-kit run --review` starts a reviewed run: the same loop, plus a review
session for each child once its local gates pass, repeated after every
correction session that follows a review (issue, local gate, or CI).

A run starts on a run target: an issue that is not a sub-issue of another
issue, as GitHub's native sub-issue relationship records it. A run target with
sub-issues is a parent issue, and its sub-issues are the children the run works
through. At startup, before the run branch is offered and before anything is
written on GitHub, Ralph asks GitHub for the issue's parent: an issue that has
one is refused, whether or not it has sub-issues of its own and whatever the
state of its parent, and the error names the parent to run instead
(`issue #124 is a sub-issue of #123: run #123 instead`). Only GitHub's "No
parent issue found" answer means the issue has no parent; any other failure of
that lookup stops the run as an error.

A run target without sub-issues is a standalone issue: the run's only child is
the issue itself, and it goes through the same loop as any child (child cycle,
corrections, review session in a reviewed run, implementation evidence,
cumulative draft PR). At startup, before the run branch is offered and before
anything is written on GitHub, a standalone issue must be authored by a trusted
author, open, and a ready child: its only triage label and only lifecycle label
`ready-for-agent`. An issue left `agent-working` by an interrupted run is also
accepted and gets the same resume-or-stop choice, or recovery when its
implementation evidence is already published, as a child. Every blocker must be
satisfied as for a child: closed, or implemented with its work on the run's
branch. Otherwise the run is refused with the reason, such as
`standalone issue #42 is not ready for an agent: labelled needs-info` or
`standalone issue #42 waits on #41, neither closed nor implemented on this branch`.
A run on the base branch offers `ralph-kit/issue-<N>` as for a parent, and the
issue, as its run's only child, gets the child session limit (by default five
sessions in a standard run, nine in a reviewed run). The draft PR is titled
`Implement Ralph issue #N`, still requires a human merge and carries no closing keyword. Once
the issue is implemented, it carries only the `implemented` lifecycle label and
its implementation evidence, the PR is marked ready, and the terminal summary is
published on the issue and the PR. Once the PR is merged, `ralph-kit close <N>`
closes the issue (see [Closure](#closure-close)). There is no parent completion: no completion
comment, no `human-review` handoff of the run target, and never `work-started`.
A stop hands the issue to human review as it would a child; relabelled
`ready-for-agent`, it resumes on the next run.

Right after marking a standalone issue `agent-working`, once every start check
has passed, Ralph also labels it with the standalone marker `standalone`, unless it already carries the
label. The label is informational only: Ralph never reads it to decide
anything, nothing removes it, and adding or removing it by hand changes nothing
about a run. A refused issue never receives it, and an implemented issue or one
handed to human review keeps it. Parent issues and their children never
receive it. If the write fails, Ralph prints a warning, repeats it in the
terminal summary, and the run continues.

At startup, before anything is commented, labelled, pushed or run, the parent
must be authored by a trusted author and must not be held back by triage: a
parent carrying any triage label configured in `ralph.toml` other than
`ready-for-agent` (by default `needs-triage`, `needs-info`, `wontfix` or
`ready-for-human`) is rejected, and the error names the parent and the labels
found. `ready-for-agent` is not
required on the parent, and a parent with no triage label starts normally.
Children's triage rules are described below.

Once a child is selected and its issue trusted, Ralph replaces its
`ready-for-agent` label with `agent-working` before the first agent session; if
that label write fails, the run stops before any session. The child keeps `agent-working` throughout its child cycle.

At the first child cycle of a run, right after marking the child
`agent-working`, Ralph also labels the parent `work-started` and removes its
`ready-for-agent` in one label edit, keeping its other labels. A parent that
already has `work-started` and no `ready-for-agent` gets no write, and later
children in the same run make none. The label is informational only: Ralph
never reads it to decide anything, and nothing cleans it up. Stops,
interruptions, crash recovery, the startup choice and the concurrency guard
leave it in place, so a stopped parent still reads as started. A run that
stops before any child cycle, or finds every child already complete, never
adds it. If the write fails, Ralph prints a warning, repeats it in the terminal
summary, and the run continues. Parent completion removes it.

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
the commits of the child's earlier child cycles that this no longer covers:
those in the gaps before the child base each child implemented on top
recorded. When the child's earlier child cycles fall in several gaps, one range
spans them all and is said to include other children's work too; if any of
those children has no recorded child base, they name every commit before the
moved child base. Local gates and required CI checks are still selected from every
path changed since the recorded child base, which the new start comment keeps
recording. If the recorded child base is no longer on the branch (it was
rebased or rewritten), Ralph starts from the current head and says so in the
comment; it does the same when the latest trusted marker is malformed. A child
without a marker starts from the current head.

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
   next runnable child. The parent ultimately goes to human review, with
   `human-review` replacing its other lifecycle labels and `work-started`;
   only a human merges.

The **child session limit** bounds the agent sessions one child may receive in
a run: by default 5 in a standard run and 9 in a reviewed run, a child's worst
case under the correction limit, plus one; `[child_session_limit]` in
`ralph.toml` sets each value (see [repository settings](#repository-settings)).
That worst case is four sessions in a standard run (one implementation session
and three correction sessions) and eight in a reviewed run (the same, with a review session after each of them); the plus
one leaves room for a backup replay, so at these values the correction limit
always binds first. A lower value stops a child earlier; a value above the
worst case has no effect beyond that room for a backup replay. Implementation,
review, issue correction, local gate correction, and CI correction sessions all
count toward it; infrastructure retries do not. Each child has its own count,
and there is no run-wide cap: the number of children does not change how many
sessions each one receives. Every run gives each child a fresh count, a child
resumed from `agent-working` included, so relabelling a handed-off child
`ready-for-agent` and rerunning gives it the full limit again. The limit counts
sessions, not tokens or time. A child that exhausts its limit is handed to
human review and stops the run, with a handoff comment such as "Ralph stopped
before review because the child session limit of 9 (reviewed run) was
exhausted.", followed by the last diagnostic or remaining findings; the next
action is to relabel it `ready-for-agent`, then rerun. The run summary and the
parent completion still report the total number of agent sessions the run used.

A standard run has no separate review session, including after corrections. The
`models.review` configuration entry is optional and unused by a standard run;
a reviewed run still requires it. A standard run
accepts `local_checks = true` or `false` and
`ci_checks = true` or `false`. Both settings are independent;
implementation evidence reports only the verification that was enabled.
The applicable gates still use the shared manifest's `before_agent_review`
selection flag, even though no separate review follows them.

With `review_by_default = true` in `ralph.toml`, `ralph-kit run` starts a
reviewed run without `--review`, and says so once at startup;
`ralph-kit run --no-review` starts a standard run instead. `--no-review` is
also accepted when the setting is off. `--review` together with `--no-review`
is a usage error. A run made reviewed by the setting requires `models.review`
and gets a reviewed run's child session limit; `--no-review` gives it a standard
run's. With the setting on,
`ralph-kit check` also checks the review role, as `check --review` does.

An implementation or correction that returns `needs_correction` must still pass
its outstanding findings to an issue correction before publication. The
correction limit and the child session limit remain in force. CI corrections
must create a new commit. Infrastructure retries do not count toward the child
session limit.

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
implementation evidence from either kind of run when its commits are on the branch,
or, when its verified head is not, when GitHub associates that head with a pull
request merged into the base branch (squash and rebase merges included) whose
merge commit is on the branch. A closed blocker is satisfied whatever its
evidence. A GitHub error during that pull request lookup goes through the
infrastructure retries, then stops the run.

## Repository settings

- `repository` is the exact GitHub `owner/name` returned by `gh repo view`.
- `base_branch` is the protected branch Ralph never commits to; it only fast-forwards it
  from the remote before creating a run branch, never when switching to an existing one.
- `remote` is the Git remote Ralph pushes to.
- `trusted_authors` is the non-empty allowlist of trusted authors for parent
  issues and children. Include the GitHub account Ralph runs as so a restarted
  run can trust its own implementation evidence.
- `review_by_default` is optional (`false` when missing). `true` makes every
  run a reviewed run, which requires `models.review`; `ralph-kit run
  --no-review` overrides it for one run.
- `caffeinate_by_default` is optional (`false` when missing). `true` makes every
  run hold a sleep lock until it ends; `ralph-kit run --no-caffeinate` overrides
  it for one run.
- `[child_session_limit]` is an optional table with two optional integer keys:
  `standard` (5 when missing, at least 1) and `reviewed` (9 when missing, at
  least 2, so a reviewed run can always reach its review session), the child
  session limit of a standard and a reviewed run. A run uses the value of its
  mode: `reviewed` with `--review` or `review_by_default = true`, `standard`
  with `--no-review` or otherwise. The correction limit already bounds a child
  at 4 sessions in a standard run and 8 in a reviewed run, so a value above
  that has no effect beyond room for a backup replay (see
  [the loop](#the-loop-run-and-run---review)); a lower value stops a child
  earlier. `ralph-kit update-config` writes the table, or the limit it leaves out:

  ```toml
  [child_session_limit]
  standard = 5
  reviewed = 9
  ```

- `labels` maps Ralph's ten label roles to labels in the target
  repository: the triage labels `needs_triage`, `needs_info`, `ready_for_agent`,
  `ready_for_human`, and `wontfix`, the lifecycle labels `ready_for_agent`,
  `agent_working`, `human_review`, and `implemented`, the parent-only
  `work_started`, and the standalone marker `standalone`. `agent_working`
  (`agent-working` by default) marks the child a run is working on; only Ralph
  sets it. `work_started` (`work-started` by default) marks a parent on which
  Ralph has started work; it is informational
  only, and Ralph never reads it to decide anything: the first child cycle of a
  run adds it in place of `ready-for-agent`, parent completion removes it, and
  nothing else cleans it up (see [the loop](#the-loop-run-and-run---review)).
  `standalone` (`standalone` by default) is the standalone marker: it marks a
  standalone issue, one with no parent and no sub-issues, on which a run has
  started. Like `work_started`, it is neither a triage label nor a lifecycle
  label, Ralph never reads it to decide anything, and nothing removes it.
  A `ralph.toml` written before `work_started` or `standalone` existed may leave
  them out and gets their default names; `check` warns about it, and
  `ralph-kit update-config` writes them. `init` and `check` offer to
  create missing ones (Yes / Later).
- `models` holds one model profile (engine, model, display name, and reasoning
  effort) each for implementation, independent review, and correction sessions.
- `models.backup` is optional: one more model profile, with the same keys, that
  takes over every session role for the rest of a run after an engine failure.
  It has one model and one reasoning effort for all roles. It may use the same
  engine as the other profiles or another one, but it must differ from each of
  them in engine, model, or reasoning effort.

The loader rejects missing and unknown settings, duplicate TOML keys, duplicate
authors, duplicate label values, unsafe repository paths, unsupported mode
values, and child session limits that are not integers or are below their
minimum before starting an agent or changing GitHub state.

## Codex and Claude Code

Each model profile accepts `engine = "codex"` or `engine = "claude"`.
Omitting `engine` defaults to Codex, preserving existing configurations. Choose
an engine separately for implementation, independent review, and corrections;
a standard run uses only the implementation and correction profiles.
Use a model name supported by the selected CLI and your account.

For example, a backup on the other engine:

```toml
[models.backup]
engine = "codex"
name = "gpt-5.6-sol"
display_name = "Sol"
reasoning_effort = "medium"
```

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
checks apply to both. Session outputs remain in the run diagnostics with the
engine and return code.

### Engine failures and the backup

An **engine failure** is a session that ends without a valid result, whatever
the cause (quota, rate limit, expired login, outage, unknown model): a Claude
Code error envelope, Claude Code output that is not valid JSON or has no
structured result, a missing Codex result file, a result that breaks the Ralph
contract, or a non-zero exit without a `committed` outcome. A non-zero exit with
a valid `committed` result is accepted. Agent outcomes (`failed`, `blocked`,
`needs_correction`, `no_changes`), rejected Git effects, and interruptions are
not engine failures.

Without `models.backup`, an engine failure stops the run safely. With one, Ralph
drops the failed session's commits and uncommitted files, so they are never
pushed, and replays the same session with the backup from the head the failed
session started from. The backup then runs every later session of the run,
whatever its role; the console announces the switch with the failed engine, its
exit detail, and the backup, confirms it once the replay returns a result, and
later session lines show the backup's engine and profile. The run diagnostics
keep the failed session and record the switch in `run.json`. The run summary, in
the terminal and on the parent issue and the cumulative draft PR, gains a line
such as `- backup: Claude Code Opus - High took over after a Codex engine failure
(review session, child #12)`, kept when the backup failed too, and has no such
line without an engine failure. The failed session and its replay both count
toward the child's session limit; when the failed session was the last one the
limit allowed, no switch happens: the console and the run summary report the
engine failure alone, such as `- engine failure: Codex in the review session
for child #12`, and the child goes to human review as for any exhausted child
session limit. A failed correction and its replay count as one
correction. An engine failure of the backup stops the run, naming both failures;
there is no second backup and no switch back. The next run starts on the regular
profiles again.

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
2/3`). Every correction also counts toward the child session limit. An exhausted
correction limit or child session limit, issue or CI corrections without a new commit,
and `blocked` or `failed` results stop the run and leave the child open with
`human-review` as its only lifecycle label. The handoff comment includes the
outstanding findings and, for local gate or CI failures, the last diagnostic.
Every handoff comment for a child whose child cycle started also says that the
child's commits stay on the branch and will count as its work if it is
relabelled `ready-for-agent`. Every handoff comment starts with a hidden
machine-readable marker, in both run modes; handoff comments posted before
Ralph wrote that marker carry none.

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
- **Resume** relabels each of them `ready-for-agent` and the run continues; each
  gets a fresh child session limit.

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

## Closure: `close`

Merging the cumulative draft PR closes nothing: its body carries no closing
keyword. Once the work is merged, a closure closes the run target as
`completed`: a parent issue's open children, then the parent itself, or a
standalone issue alone:

```sh
ralph-kit close 123
ralph-kit close --yes --config review.toml 123
ralph-kit close --force 123
```

`close` loads the configuration like the other commands (repository, label
names, trusted authors) and needs nothing else from the checkout: no branch,
clean worktree or Git remote. Before writing anything it checks that:

- the issue is open (a closed issue prints `already closed` and exits 0);
- the issue is a run target: an issue with a parent fails a check naming it
  (`#124 is a sub-issue of #123`, `close #123 instead`), whatever the parent's
  state and whether or not the issue has sub-issues of its own. As for a run,
  only GitHub's "No parent issue found" answer means no parent; any other
  failure of that lookup refuses and closes nothing;
- a parent issue carries `human-review` and is authored by a trusted author;
- each open child is authored by a trusted author and has `implemented` as its
  only lifecycle label. Closed children are ignored, whatever their labels;
- each open child with trusted implementation evidence is merged: its verified
  head is on the remote base branch, or GitHub associates it with a pull request
  merged into the base branch, whatever the merge method (squash and rebase
  merges included). The latest trusted evidence comment counts, whether a
  standard or a reviewed run wrote it (legacy formats included). `close` asks
  GitHub's compare API first (`base_branch...verified_head` is `behind` or
  `identical`), then, only when that fails, the pull requests GitHub lists for
  the verified head; it never reads local Git. A pull request closed without
  being merged, or merged into another branch, does not count.

An open child labelled `implemented` without trusted implementation evidence
(typically work a human did) does not fail a check: `close` lists it apart as
`implemented without evidence: merge not verified`.

A standalone issue, one with no parent and no sub-issues, is checked as the
run's only child: authored by a trusted author, `implemented` as its only
lifecycle label, and merged as proven by its trusted implementation evidence.
It needs no `human-review` label, and its `standalone` marker is ignored.
Unlike a child, it fails a check without trusted implementation evidence
(`#N has no implementation evidence`), so an unmerged, unimplemented or
unevidenced standalone issue is refused and only `--force` closes it. Once every
check passes, `close` lists it, asks `Close #N?` (`--yes` skips it) and closes
it alone, with one comment saying Ralph closed the standalone issue (and the
pull request that merged it, if one proved it).

A refusal lists every failed check at once and closes nothing. When every check
passes, `close` lists the children and the parent it will close and asks one
Yes/No question (No by default); `--yes` skips it, unless a child is implemented
without evidence: only a human at a terminal confirms closing that one. Without
a terminal, when the confirmation is required, it refuses. It then closes the
open children, posts one comment on the parent listing the children it closed
(naming the pull request, as `merged by PR #N`, of each child proven merged by
one; those without evidence apart, as confirmed by a human), and closes the parent. Labels are left
as they are. After a GitHub error partway, rerun the same command: children
already closed are skipped, the comment lists the children this run of `close`
closed (it is not repeated when only the parent was left open), and the closure finishes.

`--force` lets a human at a terminal close despite failed checks. Every check
still runs and each failure is printed as a warning; instead of the Yes/No
question, `close` asks you to type the issue number, and anything else closes
nothing. A forced closure is never unattended: without a terminal `--force` is
refused, and `--force` with `--yes` is a usage error. On a sub-issue with no
sub-issues of its own, or a standalone issue, `--force` closes only that issue.
The closure comment says the closure was forced and lists the failed checks. A
nonexistent issue is still refused, and a closed issue still prints `already
closed` and exits 0.

Started without an issue at a terminal, `close` lists the closable run targets,
the open issues whose closure passes every check, behind a spinner, and closes
the one picked exactly as if its number had been passed: same confirmation
(`--yes` still skips it), same rerun command. Only trusted issues in
`human-review`, or labelled `implemented` alone, are checked for the listing.
On the run branch of a listed issue (`ralph-kit/issue-<N>`), the picker
preselects it. With no closable run target, `close` says so and exits 0;
Ctrl-C in the picker closes nothing and exits 130. Without a terminal, or with
`--force`, the issue is required: a forced closure always names its issue.

Exit codes: 0 once closed, already closed, or with no closable run target; 1
for a refusal, a declined confirmation or a GitHub error; 2 for a usage error;
130 for Ctrl-C.

## Reset: `reset`

A reset returns an open run target to the state a run can start on as though
no run had happened, for instance to rerun it from scratch with a sharper issue
or another model profile:

```sh
ralph-kit reset 123
ralph-kit reset --yes --config review.toml 123
```

`reset` loads the configuration like `close`. Before writing anything it
refuses, printing every reason:

- an issue that does not exist, is authored by an untrusted author, or is closed;
- a sub-issue (`#124 is a sub-issue of #123`, `reset #123 instead`);
- a run target with a merged child, open or closed: its latest trusted
  implementation evidence has a verified head on the base branch or in a pull
  request merged into it, as for a closure. Nothing forces it;
- a run target that is not resettable: no child is `agent-working`,
  `implemented` or `human-review`, the parent carries neither
  `work-started` nor `human-review` (a standalone issue must itself be
  `agent-working`, `implemented` or `human-review`), it has no open cumulative
  draft PR, and `ralph-kit/issue-<N>` exists neither on the remote nor in the
  checkout;
- a dirty worktree while `ralph-kit/issue-<N>` is checked out, since the reset
  deletes it.

It then prints the plan: the open cumulative draft PRs to close (open pull
requests into the base branch titled `Implement Ralph parent #N` or
`Implement Ralph issue #N`), `ralph-kit/issue-<N>` to delete with its head SHA
on the remote and in the checkout, and every label change. A `⚠️` warning
flags each child still `agent-working`, since a run may still be working on it;
a pull request from another branch, which is closed but whose branch is kept;
and a run branch with no open pull request to restore it from, whose commits
only the recap's head SHA and your reflog can then recover. It asks
`Reset #N?` (No by default); `--yes` skips it, and without a terminal `reset`
needs `--yes`. Declining or Ctrl-C writes nothing.

The reset first closes each of those pull requests with a comment saying a
reset closed it, then deletes `ralph-kit/issue-<N>` on the configured remote,
then in the checkout, switching to the base branch first when it is checked
out; no other branch is ever deleted. Each child in one of those states then
gets a reset marker recording the state it left, and `ready-for-agent` as its only lifecycle label; other children,
closed ones included, are untouched. Last, the parent loses `work-started` and
its other lifecycle labels, gains `ready-for-agent`, and gets the recap reset
marker naming the closed pull requests, the deleted run branch with its head
SHA, and every child reset since its previous recap with its previous state, so
its presence means the reset finished. A standalone issue keeps its
`standalone` marker and gets a single reset marker, its recap, then
`ready-for-agent` as its only lifecycle label. Ralph reads none of its records
older than an issue's latest trusted reset marker, so the next run implements
the children from scratch from the current branch head.

After a GitHub or Git error or an interruption partway, rerun the same command:
pull requests already closed, branches already deleted, and markers and labels
already written are skipped, and the recap still lists
the children the first attempt reset. Exit codes are those of `close`: 0 once
reset; 1 for a refusal, a declined confirmation or a GitHub or Git error; 2 for
a usage error; 130 for Ctrl-C.

## Readiness check

Before launching, from the target repository root on each machine (including a VPS):

```sh
ralph-kit check
# For a reviewed run:
ralph-kit check --review
```

Commit the prepared files first. The readiness check validates
TOML and placeholders, Python and required CLI availability (and the minimum `gh` version), Git branch/remote
and clean worktree, `.ralph/` ignored by Git (any ignore source counts; the check
never edits `.gitignore`), GitHub access/repository/trusted account/write permission/labels,
the packaged result schema, the implement skill for each engine, the flags Ralph passes
to each configured engine (listed in the engine's own help, so an update that drops one
blocks before a run), and the enabled quality policy. A configured backup's engine counts
as a configured engine (CLI, implement skill, and flags), and its model name is checked
for placeholders like the others.
It inspects local gate commands and CI check names without executing them.
Disabled verification may keep placeholder entries; when both are disabled the
quality policy is not loaded.
Results are grouped by section with what was found; each failure shows the fix
(often a command to run), and checks that depend on a missing tool are reported
as not verified instead of failing. Colors are dropped when the output is not a
terminal (or with `NO_COLOR`).
Settings the configuration leaves out and that take their default value are
listed as information (`ℹ`), pointing to `ralph-kit update-config`; they never fail
the check. Defaulted label roles are a warning instead, since the label may be
missing on GitHub.
Labels whose colour differs from their label role's fixed colour are a warning,
with the `gh label edit … --color …` commands as the fix.
A nonzero exit code means a blocking check failed. In a terminal, missing labels
are offered for creation after the report (Yes / Later), then labels with the
wrong colour are offered for recolouring (colour only, never name or description);
those are the only GitHub writes. Engines only print their help; no agent session,
project check, CI run, push, issue or PR mutation occurs. The configured
quality module is imported: it must be trusted and have no import-time side effects.

A successful readiness check does not validate agent authentication/model access,
actual skill discovery or transitive dependencies, Git push credentials, project
dependencies and services, test results, actual CI workflow names, or the readiness
of the parent issue, its children and their blockers. Those remain the user's
responsibility, along with accepting the agents' permission-bypass options and
reviewing the resulting PR before merge.
