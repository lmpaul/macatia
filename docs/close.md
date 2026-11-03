# Closure: `close`

```sh
macatia close                # at a terminal, pick among the closable run targets
macatia close 123            # closure: close #123's children, then #123, or standalone #123 alone (asks first)
macatia close --yes 123      # same, without the confirmation
macatia close --force 123    # close despite failed checks, after typing 123
```

Merging the cumulative draft PR closes nothing: its body carries no closing
keyword. Once the work is merged, a closure closes the run target as
`completed`: a parent issue's open children, then the parent itself, or a
standalone issue alone:

```sh
macatia close 123
macatia close --yes --config review.toml 123
macatia close --force 123
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
it alone, with one comment saying Macatia closed the standalone issue (and the
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
On the run branch of a listed issue (`macatia/issue-<N>`), the picker
preselects it. With no closable run target, `close` says so and exits 0;
Ctrl-C in the picker closes nothing and exits 130. Without a terminal, or with
`--force`, the issue is required: a forced closure always names its issue.

Exit codes: 0 once closed, already closed, or with no closable run target; 1
for a refusal, a declined confirmation or a GitHub error; 2 for a usage error;
130 for Ctrl-C.
