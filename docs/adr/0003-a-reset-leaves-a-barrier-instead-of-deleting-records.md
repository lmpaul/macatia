# A reset leaves a barrier instead of deleting Ralph's records

Ralph reads its own past comments to decide what to do: the latest child base marker is reused while its commit is on the branch, implementation evidence lets a run recover a published child, and closure reads the latest evidence. A reset must stop those records from counting, so it leaves a reset marker on the run target and on each child it makes ready for an agent again, and Ralph reads none of its own records older than the latest reset marker on the same issue. Deleting the records is offered only as a purge, which a person asks for explicitly.

## Considered Options

- **Delete Ralph's comments by default.** Rejected as the default: the handoff comments that send a child to human review carried no marker before resets existed, and Ralph posts as a trusted author, so those comments cannot be told apart from the person's own; deletion also erases the history a human may want when deciding why the run was reset. It stays available as a purge, because the barrier hides nothing from the agent sessions that read the issue themselves.
- **Minimize the comments as outdated.** Rejected: anyone can unminimize a comment, so it is not a signal Ralph can rely on.
- **A single barrier on the run target.** Rejected: Ralph reads each child's records on the child's own issue.

## Consequences

The reset marker is persisted in GitHub comments like the other markers, so its text must never change, and every reader of a Ralph record (run, recovery, closure) must respect it.
