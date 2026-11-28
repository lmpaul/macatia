# macatia

Macatia drives coding agents through a run target's children, one at a time, and publishes the verified work as a draft PR for a human to review and merge.

## Work structure

**Run target**:
An issue a run can start on: one that is not a sub-issue of another issue, whether it is a parent issue or a standalone issue.
_Avoid_: Runnable issue, eligible issue

**Startable run target**:
A run target a run would start on now and that has work left: a parent issue that passes the run's startup checks and has a ready child or a child left agent working, or a standalone issue that is itself ready or agent working and waits on no unsatisfied blocker. A run started without a run target offers these to choose from.
_Avoid_: Eligible issue, available issue, candidate

**Closable run target**:
A run target whose closure passes every check now, so `close` would close it without `--force`. A closure started without a run target offers these to choose from.
_Avoid_: Finished issue, done issue, candidate

**Parent issue**:
A run target with sub-issues; its sub-issues are the children Macatia works through.
_Avoid_: Epic, spec issue

**Standalone issue**:
A run target without sub-issues; Macatia works on the issue itself.
_Avoid_: Single issue, leaf issue

**Child**:
The unit of work one implementation covers: a sub-issue of a parent issue, or a standalone issue itself.
_Avoid_: Sub-issue, ticket, task

**Blocker**:
A child that another child depends on and that must be implemented first.
_Avoid_: Prerequisite, dependency

**Remaining child**:
A child that is open and not yet implemented, whatever its other labels.
_Avoid_: Incomplete child, open child

**Ready child**:
A remaining child whose only triage label and only lifecycle label is ready-for-agent, blocked or not.
_Avoid_: Pending child, queued child

**Satisfied blocker**:
A blocker that is closed, or implemented with its work present on the run's branch: its verified head is on that branch, or the merge of a pull request into the base branch that contains it is.
_Avoid_: Done blocker, resolved dependency

**Runnable child**:
A ready child with no unsatisfied blocker.

**Dependency deadlock**:
The state where remaining children exist but every one is waiting on a blocker that cannot complete.

**Trusted author**:
A GitHub account whose issues and evidence comments Macatia accepts; everything else is ignored.

## Running

**Loop**:
The cycle Macatia repeats for each runnable child: implement, verify, correct if needed, publish, then select the next runnable child.
_Avoid_: Pipeline, workflow

**Child cycle**:
The part of the loop that takes one runnable child from its implementation session to a verified head or to human review; publication of implementation evidence follows it.
_Avoid_: Attempt, completion

**Child base**:
The branch commit a child's work starts from; its implementation evidence and review session cover every commit after it.
_Avoid_: Issue base, starting commit

**Run**:
One execution of the loop against a run target, until no runnable child is left or the run stops.
_Avoid_: Runner, execution

**Run branch**:
The branch a run commits to and publishes from. A run started on the base branch offers to switch to `macatia/issue-<run target>`, creating it from an up-to-date base branch unless it already exists.
_Avoid_: Work branch, feature branch, Macatia branch

**Run worktree**:
The separate checkout of a run target's run branch, where a run works away from the person's own checkout.
_Avoid_: Worktree (alone), sandbox, isolated checkout

**Standard run**:
A run whose loop has no review session.
_Avoid_: Implementation-only loop, experimental loop

**Reviewed run**:
A run whose loop adds review sessions, in one of two review modes: child review or final review.
_Avoid_: Original loop, review loop (there is only one loop)

**Review mode**:
How a reviewed run reviews its work, chosen in the repository's settings rather than per run: child review or final review, never both.
_Avoid_: Review type, review kind

**Child review**:
The review mode that adds a review session for each child, once its work passes the local gates and again after every correction session that follows a review.
_Avoid_: Per-ticket review, per-child review

**Final review**:
The review mode that leaves children unreviewed, as in a standard run, and reviews the run target's whole work once every child is implemented, checking that the work is complete and done well; a green CI says nothing of either.
_Avoid_: End-of-run review, issue review, final CI check (a different thing)

**Sleep lock**:
The hold a run keeps on the machine so it does not idle-sleep until the run ends; when the machine cannot provide one, the run continues without it.
_Avoid_: Caffeinate, keep-awake, wake lock

**Persistent terminal**:
The terminal a run continues in after the person's own terminal disconnects; a run started outside one can restart itself in one named after the repository and the run target.
_Avoid_: tmux session, session (alone), detached run

**Agent session**:
One fresh, non-interactive invocation of a coding agent with a single role: an implementation session, a review session, a final review session, or a correction session.
_Avoid_: Iteration, agent run

**Implementation session**:
The agent session that first implements a child.

**Review session**:
An agent session that independently checks a child's work against its whole issue, fixes what it can and commits, and reports as findings only the gaps that remain. Only reviewed runs in child review start one.
_Avoid_: Review (alone), code review

**Final review session**:
An agent session that independently checks the whole work of a run target, every child's commits on the run branch, against the run target and all its children, fixes what it can and commits, and reports as findings only the gaps that remain. It belongs to the run, not to any child; only reviewed runs in final review start one.
_Avoid_: Review (alone), final check, end-of-run review

**Correction session**:
An agent session that repairs a child's work, or a run's work after its final review or final CI check fails, and must produce a new commit: an issue correction, a local gate correction, a CI correction, a final review correction, or a final CI correction. A correction session without a new commit sends the child, or for a final review or final CI correction the run target, to human review.
_Avoid_: Fix session, retry

**Issue correction**:
A correction session that addresses the findings of an implementation, review, or correction session that returned `needs_correction`.

**Local gate correction**:
A correction session that repairs a failed local gate.

**CI correction**:
A correction session that repairs a CI check that failed on a child's work.

**Final review correction**:
A correction session that addresses the findings a final review session left; it belongs to the run, not to any child, and another final review session follows it.
_Avoid_: Issue correction (that one belongs to a child), run correction

**Final CI correction**:
A correction session that repairs a CI check that failed in a final CI check; it belongs to the run, not to any child.
_Avoid_: Run correction, CI correction (for this one)

## Limits

**Child session limit**:
The maximum number of agent sessions one child may receive in a run, with one value for standard runs and runs in final review, and one for runs in child review; counted in sessions, never tokens or time. Each run gives a child a fresh count.
_Avoid_: Run budget, ticket budget, session budget, iteration limit, token budget

**Correction limit**:
The maximum number of correction sessions one child may receive (three), counting issue, local gate, and CI corrections together. A run's final review corrections and its final CI corrections each have their own limit of three, counted toward no child.
_Avoid_: Attempts, retry limit, issue-correction limit

**Infrastructure retry**:
A repeated remote operation (GitHub, CI polling) that does not count toward the child session limit. Missing CI on a pull request that conflicts with its base is not retried: GitHub Actions never starts it.
_Avoid_: Retry (alone), correction

**Engine**:
The coding-agent CLI behind a session: Codex or Claude Code.
_Avoid_: Provider, backend

**Model profile**:
The engine, model, and reasoning effort an agent session runs with: one for each session role, plus the backup when configured.

**Backup**:
The single optional model profile that takes over every session role for the rest of a run after an engine failure; its engine may differ from or match the others.
_Avoid_: Fallback, backup engine, backup model, secondary profile

**Engine failure**:
An agent session that ends without a valid result, whatever the cause: quota, rate limit, login, outage, or an unknown model.
_Avoid_: Crash, engine error

## Agent results

**Outcome**:
The status an agent session reports: `committed`, `no_changes`, `needs_correction`, `blocked`, or `failed`.
_Avoid_: Status, result code

**Summary**:
The session's short report of what it did and which checks passed.

**Finding**:
One unresolved, actionable gap tied to an issue requirement; never a completed change or a passed check.
_Avoid_: Issue, problem, comment

## Verification

**Verification**:
The automated proof a child's commit is sound: local gates and/or CI checks, each enabled independently.
_Avoid_: Checks, mode

**Local gate**:
A project command Macatia runs in the checkout before publishing.
_Avoid_: Local check, test step

**CI check**:
A required CI result that must pass on the exact published commit.
_Avoid_: CI gate, status

**Final CI check**:
The single wait for CI checks on a run's final head, for a run that does not wait for them after each child. Each child's own CI result then serves only as a hint to the correction that follows a failure.
_Avoid_: Deferred CI, end-of-run CI, last CI

**Per-child CI history**:
For each child implemented in a run, in order, the CI state of its verified head (green, red, pending, cancelled, or missing), read when a final CI check fails; its first red child is named as a hint, never as the cause.
_Avoid_: Blame, culprit

**Quality policy**:
The repository's quality module and manifest, which define the local gates and required CI checks.
_Avoid_: Quality config

**Readiness check**:
The pre-flight inspection (`macatia check`) that a repository and machine can host a run, without running agents or project checks.
_Avoid_: Check, doctor, preflight

## Publication and handoff

**Cumulative draft PR**:
The single draft pull request that collects every child's commits for a run's branch.
_Avoid_: PR per child

**Implementation evidence**:
The trusted comment on a child recording which commits implemented and verified it.
_Avoid_: Success comment, marker

**Final review evidence**:
The trusted comment on a run target recording the head that a final review session found complete with no finding left; it holds only for that head, so a run that finds a different head reviews again.
_Avoid_: Review comment, final review marker, approval

**Verified head**:
The commit on which verification passed and which was published.

**Agent working**:
The lifecycle state of a child a run has started and has neither implemented nor handed to human review; only Macatia sets it. A run that ends without releasing its child leaves it in this state.
_Avoid_: In progress, WIP, claimed, running

**Implemented**:
The lifecycle state of a child whose work is complete and verified; only Macatia sets it.
_Avoid_: Done, closed

**Human review**:
The lifecycle state of a child or parent handed to a human: either because Macatia stopped on it, or because all children are implemented. Macatia never resumes a child in this state.
_Avoid_: Escalation, stuck

**Handoff comment**:
The trusted comment Macatia posts on a child, or on the run target, when it sends it to human review, saying why it stopped. Since resets exist, each one carries a hidden marker so a purge can find it; earlier ones carry none.
_Avoid_: Stop comment, failure comment

**Work started**:
The label role marking a parent on which Macatia has started work. It is neither a triage label nor a lifecycle label, and it is informational only: Macatia never reads it to decide anything.
_Avoid_: Parent in progress, parent agent-working

**Standalone marker**:
The label role marking a standalone issue on which a run has started; it is never removed. Like work started, it is neither a triage label nor a lifecycle label, and Macatia never reads it to decide anything.
_Avoid_: Standalone label, single-issue flag

**Label role**:
One of Macatia's ten canonical meanings for a label, mapped to a label name in each repository. Each label role has a fixed colour, chosen by who must act next, whatever name the repository gives it.

**Triage labels**:
The label roles that say whether a child is ready for an agent: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. A run also refuses to start on a parent carrying any of them other than ready-for-agent.

**Lifecycle labels**:
The label roles that track a child's progress through a run: ready-for-agent, agent-working, implemented, and human-review.

**Run diagnostics**:
The local, redacted record of a run's prompts, session results, and command output, which later sessions in the same checkout can read.
_Avoid_: Logs, artifacts

## Closing

**Merged child**:
A child whose verified head is on the base branch, or belongs to a pull request merged into the base branch, whatever the merge method.
_Avoid_: Landed child, done child

**Closure**:
Closing a run target once a human has merged its work: a parent issue's open children, then the parent itself, or a standalone issue alone.
_Avoid_: Parent closure, finish, complete, done (completed is a run's outcome)

## Resetting

**Reset**:
Returning an open run target with no merged child to the state a run can start on as though no run had happened: the run target and its agent working, implemented, or human review children ready for an agent again, its run branch and cumulative draft PR gone, and the records of its earlier runs no longer read by Macatia. The standalone marker stays.
_Avoid_: Rollback, undo, clean, rewind, restart

**Resettable run target**:
A run target a reset would act on now: open, with no merged child, and carrying the trace of a run: a child agent working, implemented, or in human review, a parent marked work started or human review, a run branch named after it, or an open cumulative draft PR; for a purge, a reset marker, or any other of Macatia's own comments left on it, is such a trace too. A reset started without a run target offers these to choose from.
_Avoid_: Stale issue, failed issue, candidate

**Reset marker**:
The trusted comment a reset leaves on the run target and on each child it made ready for an agent again; Macatia reads none of its own records older than it on that issue. The one on the run target recaps what the reset undid.
_Avoid_: Reset comment, barrier

**Purge**:
A reset that deletes Macatia's own comments on the run target and its children instead of leaving reset markers, so that no later agent session reads them. The issues' timelines and the closed cumulative draft PR remain.
_Avoid_: Hard reset, full reset, wipe
