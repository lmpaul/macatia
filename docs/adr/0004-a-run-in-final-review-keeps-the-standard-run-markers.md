# A run in final review keeps the standard run's markers

A run in final review treats every child exactly as a standard run does, so it writes the standard run's implementation evidence. Its parent completion comment and terminal summary also carry the standard run's markers (`macatia-experimental-parent-completion` and `macatia-experimental-terminal-summary`) rather than markers of their own. The final review leaves its own record instead: final review evidence on the run target, with the new `macatia-final-review-evidence` marker recording the reviewed head.

## Considered Options

- **New parent completion and terminal summary markers for a run in final review.** Rejected: these markers are read only to keep the parent completion idempotent, to let recovery skip terminal summaries published after implementation evidence, and to let a purge find Macatia's comments. Each of those readers lists the markers it knows, so a new pair would have to be added to all of them for no decision that depends on it. Whether a run target's work was reviewed as a whole is answered by final review evidence for its head, not by which marker a completion comment carries.
- **The child review markers.** Rejected: they mark a run whose children were each reviewed, which a run in final review never does.

## Consequences

Marker values are persisted in GitHub comments, so once released they never change. A reader cannot tell from a parent completion or terminal summary marker alone whether the run was in final review; the text of those comments names the final review, and final review evidence records it.
