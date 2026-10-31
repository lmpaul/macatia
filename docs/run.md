# The loop: `run` and `run --review`

```sh
ralph-kit run                  # at a terminal, pick among the startable run targets
ralph-kit run 123              # standard run: implement the children of parent issue #123
ralph-kit run --review 123     # reviewed run: adds an independent review session per child
ralph-kit run --no-review 123  # standard run, even when review_by_default = true
ralph-kit run --caffeinate 123 # keep the machine awake until the run ends
ralph-kit run --no-caffeinate 123 # let it sleep, even when caffeinate_by_default = true
ralph-kit run --tmux 123       # offer to restart the run in a persistent terminal (tmux)
ralph-kit run --worktree 123   # run in a run worktree, away from your own checkout
```

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
closes the issue (see [Closure](close.md)). There is no parent completion: no completion
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
[ADR 0001](adr/0001-child-base-falls-back-when-another-child-lands-on-top.md)).
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
`ralph.toml` sets each value (see [repository settings](configuration.md#repository-settings)).
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

## The run branch

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

## Keep the machine awake: `--caffeinate`

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

## Persistent terminal: `--tmux`

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

## Run worktree: `--worktree`

`--worktree` runs in a run worktree: a separate checkout of the run target's
run branch, so you can keep working in your own checkout while the run goes.
Your checkout may be dirty and on any branch; nothing in it changes, not even
its local base branch.

The run worktree of run target N is always at
`<parent of the main checkout>/<main checkout directory name>.worktrees/issue-N`:
for a repository cloned at `~/code/buyr`, `~/code/buyr.worktrees/issue-123`.
The main checkout is found through Git, so the path is the same whichever
checkout of the repository the run starts from.

Once the run target passes its startup checks, in place of the run branch
offer, and before the `--tmux` question, Ralph fetches the base branch, then
asks "Create a run worktree for ralph-kit/issue-N at ‹path›?":

- **Create** (the default) adds the run worktree on a new run branch
  `ralph-kit/issue-N` from `<remote>/<base>`, releases the sleep lock, then
  restarts the run in the run worktree, in this terminal. The restarted run has
  the same `--review`, `--caffeinate`, `--config` and `--worktree` flags, never
  `--tmux`, and the run target, the one picked when the run started without
  one. It runs the usual startup checks in the run worktree, reads its
  configuration and keeps its run diagnostics there, and pushes the run branch.
- **Stop**, or Ctrl-C at the prompt, ends the run with nothing changed.

When the run branch already exists, locally, on the remote or both, but is
checked out nowhere, as after a run without `--worktree`, Ralph asks the same
question without fetching the base branch, and Create adds the run worktree on
that run branch, tracking the remote one: a run branch only on the remote is
checked out tracking it, and a local run branch behind the remote one is
fast-forwarded. A local run branch that has diverged from the remote one stops
the run before the question, with nothing rewritten.

When the run worktree already exists at that path on the run branch, as after a
stopped or interrupted run, Ralph asks "Continue in the existing run worktree at
‹path›?" instead:

- **Continue** (the default) has the run branch follow the remote one, as the
  switch to an existing run branch does: a run branch behind the remote one is
  fast-forwarded, and one that has diverged from it stops the run with nothing
  rewritten. Ralph then restarts the run in the run worktree, as for Create.
- **Stop**, or Ctrl-C at the prompt, ends the run with nothing changed.

A run started with `--worktree` from inside the run worktree of its run target,
as the restarted run is, asks nothing and continues in place. A stopped run's
rerun command keeps `--worktree`, so the same command resumes the run from your
own checkout or from the run worktree. Started from inside the run worktree of
another run target, `--worktree` works as from your own checkout: the path is
always derived from the main checkout.

The run stops with nothing changed:

- without a terminal, naming the run worktree it would create or continue in
  and the command to run in a terminal to do so;
- when the selected configuration file is not where the run worktree reads it,
  `<remote>/<base>` for a new run branch, or the run branch otherwise: commit
  and push it, or run without `--worktree`;
- when the run branch is checked out in another checkout, such as your own,
  naming that checkout: switch it to another branch there, or run without
  `--worktree`;
- when the run worktree path already exists, but not as this repository's run
  worktree on the run branch.

Without ISSUE, the run target is picked first, in your checkout. Without
`--worktree`, nothing is asked and the run works in your checkout as above.

Nothing removes a run worktree yet. Once you are done with it, remove it by
hand from your own checkout, then delete its run branch:

```sh
git worktree remove ../buyr.worktrees/issue-123
git branch -d ralph-kit/issue-123
```

`git worktree remove` refuses a run worktree with uncommitted changes, and
`git branch -d` a run branch that is not merged; check them before forcing
either.
