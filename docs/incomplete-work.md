# Incomplete issue work

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
does not discard correction instructions. Macatia redacts credentials from findings
before passing them to correction sessions or publishing them on GitHub.

When implementation, independent review, or a CI correction returns
`needs_correction`, Macatia starts a fresh issue correction, preserves the existing commits, and requires
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
relabelled `ready-for-agent`. When the run stops on a child, or is interrupted,
before that child's commits were published, Macatia first pushes the run branch
and opens the cumulative draft PR if there is none, so a human can review the
unverified commits there. If the push fails, the child is still handed over and
its handoff comment says the commits exist only in the run's local checkout.
Every handoff comment starts with a hidden machine-readable marker, in both run
modes; handoff comments posted before Macatia wrote that marker carry none.

Macatia publishes implementation evidence and adds `implemented` only after the
issue review and configured verification succeed for the final commit. A child
already marked `human-review` is not resumed automatically; remove that label
and restore `ready-for-agent` when it is ready for another run.

A run that ends without releasing its child (a crash, an error stop, or a killed
process) leaves it `agent-working`. At startup, before any agent session, a run
first marks `implemented` every child whose implementation evidence was already
published, without asking. If children of the parent are still `agent-working`
after that, Macatia shows one warning naming them all: it cannot tell an
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
Without one (`nohup`, a scheduler, redirected output), Macatia takes the Stop path
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
interrupt a run the same way. Macatia ends the agent session in flight,
terminating its engine process, and releases the child it was working on. If the
branch head still equals the commit its child cycle started from, nothing was
committed for it in that child cycle:
the child goes back to `ready-for-agent` and the next run starts it again.
Otherwise the child goes to `human-review` with a handoff comment naming the
unverified commit range `<base>..<head>`; inspect those commits, then close the
child or relabel it `ready-for-agent` and rerun. A child whose implementation
evidence was already published is marked `implemented` instead. Macatia then
publishes the terminal summary with the outcome `interrupted` on the parent (and
the draft PR, if one exists), listing that child with its reason and next
action, prints it, and exits with the shell's code for that signal: 130, 143,
or 129. An interruption between two children releases nothing and publishes the
same summary. A second interruption during this cleanup exits immediately,
leaving the labels as they are, so the next run asks about a child still
`agent-working` as described above. `kill -9`, crashes, and power loss are never
caught: they release nothing and publish no interrupted summary. A signal
ignored at launch, such as SIGHUP under `nohup`, stays ignored.
