# Reset: `reset`

```sh
macatia reset                # at a terminal, pick among the resettable run targets
macatia reset 123            # reset: make #123 and its children ready for an agent again (asks first)
macatia reset --yes 123      # same, without the confirmation
macatia reset --purge 123    # same, deleting Macatia's comments instead of leaving reset markers
```

A reset returns an open run target to the state a run can start on as though
no run had happened, for instance to rerun it from scratch with a sharper issue
or another model profile:

```sh
macatia reset 123
macatia reset --yes --config review.toml 123
macatia reset --purge 123
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
  draft PR, and `macatia/issue-<N>` exists neither on the remote nor in the
  checkout;
- a dirty worktree while `macatia/issue-<N>` is checked out, since the reset
  deletes it;
- a dirty run worktree, untracked files included, or a locked one, since the
  reset removes it;
- a reset started from inside the run worktree of the run target, which it
  would remove: run it from outside, for instance from your own checkout.

The run worktree of the run target is the checkout, other than the main one,
where `macatia/issue-<N>` is checked out, as `macatia run --worktree`
creates it; the reset finds it by that run branch, wherever it is. A run target
without a run worktree resets as though run without `--worktree`.

It then prints the plan: the open cumulative draft PRs to close (open pull
requests into the base branch titled `Implement Macatia parent #N` or `Implement
Macatia issue #N`), the run worktree to remove, `macatia/issue-<N>` to delete
with its head SHA
on the remote and in the checkout, and every label change. A `⚠️` warning
flags each child still `agent-working`, since a run may still be working on it;
a pull request from another branch, which is closed but whose branch is kept;
and a run branch with no open pull request to restore it from, whose commits
only the recap's head SHA and your reflog can then recover. It asks
`Reset #N?` (No by default); `--yes` skips it, and without a terminal `reset`
needs `--yes`. Declining or Ctrl-C writes nothing.

Without `ISSUE`, at a terminal, `reset` offers the resettable run targets by
ascending number, each named as a parent with its count of children to reset
or as a standalone issue: the open issues the checks above accept. It finds
them with one listing of the remote's `macatia/issue-` heads, one of the open
cumulative draft PRs, and the open issues' labels, then inspects each run target
they point to as a reset of it would. On the run branch of a listed issue
(`macatia/issue-<N>`), the picker preselects it. With no resettable run
target, `reset` says so, naming the repository, and exits 0; Ctrl-C in the
picker resets nothing and exits 130. Without a terminal, the issue is required.

The reset first closes each of those pull requests with a comment saying a reset
closed it, then removes the run worktree with `git worktree remove`, ignored
files in it (dependencies, run diagnostics) going with it, then deletes
`macatia/issue-<N>` on the configured remote, then in
the checkout, switching to the base branch first when it is checked out; no
other branch is ever deleted. Each child in one of those states then gets a
reset marker recording the state it left, and `ready-for-agent` as its only
lifecycle label; other children, closed ones included, are untouched. Last, the
parent loses `work-started` and its other lifecycle labels, gains
`ready-for-agent`, and gets the recap reset marker naming the closed pull
requests, the deleted run branch with its head SHA, and every child reset since
its previous recap with its previous state, so its presence means the reset
finished. A standalone issue keeps its `standalone` marker and gets a single
reset marker, its recap, then `ready-for-agent` as its only lifecycle label.
Macatia reads none of its records older than an issue's latest trusted reset
marker, so the next run implements the children from scratch from the current
branch head.

`--purge`, with or without `ISSUE`, deletes Macatia's own comments instead of
leaving reset markers, so later agent sessions, which read the issues
themselves, no longer see them. Where a reset posts a reset marker it deletes
every trusted comment carrying a Macatia marker: implementation evidence in every
format, child base markers, handoff markers, parent completion, terminal
summary, closure and reset markers. It touches the run target and each child
still open that is `agent-working`, `implemented` or `human-review`, or
`ready-for-agent` and carrying such a comment, as an earlier reset left it; labels change, in the same order, as for a reset, and it leaves no
reset marker. Untrusted comments, trusted comments without a Macatia marker
(handoff comments from before the handoff marker among them), and pull request
comments are untouched. The plan counts the comments it deletes on each issue.
For a purge, a Macatia comment on the run target, such as an earlier reset's
recap, is the trace of a run too, so a reset run target can still be purged
later, and the picker also offers the trusted open issues carrying a Macatia
comment.

After a GitHub or Git error or an interruption partway, rerun the same command:
pull requests already closed, a run worktree already removed, branches already
deleted, and markers and labels already written are skipped, and the recap still lists the children the first
attempt reset, and the pull request it closed from `macatia/issue-<N>` with
the branch it deleted, which a hidden marker in the closing comment records.
Exit codes are those of `close`: 0 once reset or with no resettable run
target; 1 for a refusal, a declined confirmation or a GitHub or Git error; 2 for
a usage error; 130 for Ctrl-C.
