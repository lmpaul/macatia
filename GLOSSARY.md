# ralph-kit

Ralph drives coding agents through a parent issue's children, one at a time, and publishes the verified work as a draft PR for a human to review and merge.

## Work structure

**Parent issue**:
The GitHub issue that groups the work for one run; its sub-issues are the children Ralph works through.
_Avoid_: Epic, spec issue

**Child**:
A sub-issue of the parent issue: the unit of work one implementation covers.
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

**Runnable child**:
A ready child with no unsatisfied blocker.

**Dependency deadlock**:
The state where remaining children exist but every one is waiting on a blocker that cannot complete.

**Trusted author**:
A GitHub account whose issues and evidence comments Ralph accepts; everything else is ignored.

## Running

**Loop**:
The cycle Ralph repeats for each runnable child: implement, verify, correct if needed, publish, then select the next runnable child.
_Avoid_: Pipeline, workflow

**Child cycle**:
The part of the loop that takes one runnable child from its implementation session to a verified head or to human review; publication of implementation evidence follows it.
_Avoid_: Attempt, completion

**Child base**:
The branch commit a child's work starts from; its implementation evidence and review session cover every commit after it.
_Avoid_: Issue base, starting commit

**Run**:
One execution of the loop against a parent issue, until no runnable child is left or the run stops.
_Avoid_: Runner, execution

**Standard run**:
A run whose loop has no review session.
_Avoid_: Implementation-only loop, experimental loop

**Reviewed run**:
A run whose loop adds a review session for each child, once its work passes the local gates and again after every correction session that follows a review.
_Avoid_: Original loop, review loop (there is only one loop)

**Sleep lock**:
The hold a run keeps on the machine so it does not idle-sleep until the run ends; when the machine cannot provide one, the run continues without it.
_Avoid_: Caffeinate, keep-awake, wake lock

**Agent session**:
One fresh, non-interactive invocation of a coding agent with a single role: an implementation session, a review session, or a correction session.
_Avoid_: Iteration, agent run

**Implementation session**:
The agent session that first implements a child.

**Review session**:
An agent session that independently checks a child's work against its whole issue, fixes what it can and commits, and reports as findings only the gaps that remain. Only reviewed runs start one.
_Avoid_: Review (alone), code review

**Correction session**:
An agent session that repairs a child's work and must produce a new commit: an issue correction, a local gate correction, or a CI correction. A correction session without a new commit sends the child to human review.
_Avoid_: Fix session, retry

**Issue correction**:
A correction session that addresses the findings of an implementation, review, or correction session that returned `needs_correction`.

**Local gate correction**:
A correction session that repairs a failed local gate.

**CI correction**:
A correction session that repairs a failed CI check.

## Limits

**Run budget**:
The maximum number of agent sessions a run may start, shared by all its children; counted in sessions, never tokens or time. Without an explicit value it is five sessions for each ready child in a standard run and nine in a reviewed run (a child's worst case under the correction limit, plus one), never fewer than four; the ready children are counted when the run starts, including children it resumed from agent working.
_Avoid_: Session budget, agent budget, iteration limit, token budget

**Correction limit**:
The maximum number of correction sessions one child may receive (three), counting issue, local gate, and CI corrections together.
_Avoid_: Attempts, retry limit, issue-correction limit

**Infrastructure retry**:
A repeated remote operation (GitHub, CI polling) that does not consume the run budget.
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
A project command Ralph runs in the checkout before publishing.
_Avoid_: Local check, test step

**CI check**:
A required CI result that must pass on the exact published commit.
_Avoid_: CI gate, status

**Quality policy**:
The repository's quality module and manifest, which define the local gates and required CI checks.
_Avoid_: Quality config

**Readiness check**:
The pre-flight inspection (`ralph-kit check`) that a repository and machine can host a run, without running agents or project checks.
_Avoid_: Check, doctor, preflight

## Publication and handoff

**Cumulative draft PR**:
The single draft pull request that collects every child's commits for a run's branch.
_Avoid_: PR per child

**Implementation evidence**:
The trusted comment on a child recording which commits implemented and verified it.
_Avoid_: Success comment, marker

**Verified head**:
The commit on which verification passed and which was published.

**Agent working**:
The lifecycle state of a child a run has started and has neither implemented nor handed to human review; only Ralph sets it. A run that ends without releasing its child leaves it in this state.
_Avoid_: In progress, WIP, claimed, running

**Implemented**:
The lifecycle state of a child whose work is complete and verified; only Ralph sets it.
_Avoid_: Done, closed

**Human review**:
The lifecycle state of a child or parent handed to a human: either because Ralph stopped on it, or because all children are implemented. Ralph never resumes a child in this state.
_Avoid_: Escalation, stuck

**Label role**:
One of Ralph's eight canonical meanings for a label, mapped to a label name in each repository.

**Triage labels**:
The label roles that say whether a child is ready for an agent: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix.

**Lifecycle labels**:
The label roles that track a child's progress through a run: ready-for-agent, agent-working, implemented, and human-review.

**Run diagnostics**:
The local, redacted record of a run's prompts, session results, and command output, which later sessions in the same checkout can read.
_Avoid_: Logs, artifacts

## Closing

**Merged child**:
A child whose verified head is on the base branch.
_Avoid_: Landed child, done child

**Parent closure**:
Closing a parent issue's open children, then the parent itself, once a human has merged their work.
_Avoid_: Finish, complete, done (completed is a run's outcome)
