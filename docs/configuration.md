# Configuration

```sh
macatia init                 # writes ./macatia.toml (detects what it can, asks the rest)
macatia init --config b.toml  # writes ./b.toml instead, for a second configuration
macatia configure            # change sections of ./macatia.toml from a menu
macatia update-config        # add the defaulted settings ./macatia.toml leaves out, then their GitHub labels
```

## Create a configuration: `init`

`init` first requires `git`, an authenticated `gh` 2.70.0 or newer and at least one agent CLI
(`claude` or `codex`), and stops before any question otherwise. `check` repeats
these requirements, requiring the CLI of each configured engine, so it is the command
to run when reusing a committed `macatia.toml` on another machine.
`init` then reads the GitHub repository, default branch, remote and current `gh`
account, and enables local gates and CI checks when `.github/scripts/quality.py` and
`.github/quality-gates.json` exist. It writes `final_ci_check` (see
[the final CI check](verification.md#final-ci-check)) with the value it gives
`ci_checks` and a comment explaining it, without asking. It asks only for what it could not detect,
plus the agent engine, models and reasoning efforts; lists are navigated with
the arrow keys. Only installed agent CLIs are offered. Codex models and their supported efforts come from your account's
catalog (`codex debug models`, with a free-text fallback); Claude Code has no
model listing, so `init` offers its documented aliases (`fable`, `opus`,
`sonnet`, `haiku`), which always follow the latest version. Without a terminal
(agents, CI) it asks nothing and leaves placeholders that `macatia check` reports.
After the model profiles, `init` asks for a backup (see
[Engine failures and the backup](engines.md#engine-failures-and-the-backup)) among the installed
agent CLIs, plus "No backup". It recommends and preselects the other installed CLI,
since an engine failure is rarely limited to one model, and preselects "No backup"
when only one CLI is installed. A chosen backup gets the same model and reasoning
effort questions, and must differ from the other profiles: a reasoning effort that
would make it identical to a session role's model profile is shown greyed out with
the reason, a model only when all its efforts would, and a model name typed by hand
is checked right after you enter it.
`--config path/to/other.toml` selects another configuration, relative to the
repository root: `init` writes it (its directory must exist, and `./macatia.toml` is left
untouched), and `run` and `check` read it. `init` refuses an existing file unless
`--force` is given, which overwrites only that file; its final message suggests
`macatia check --config path/to/other.toml`. Macatia writes run
diagnostics to `.macatia/`. Unless Git already ignores it, `init` appends `.macatia/` to the
repository's `.gitignore` (creating the file if needed) and says so. Commit it with `macatia.toml`;
`check` reports `.macatia/` when Git does not ignore it.

Commit `macatia.toml` (and `.gitignore` when `init` changed it) and run on a clean worktree.

## Change a configuration: `configure`

`init` on an existing file points to `configure`: `macatia configure [--config PATH]`
opens an existing, valid configuration and shows a menu of its sections (model
profiles, backup, trusted authors, verification, Git, reviewed runs by default, keep the
machine awake by default, child session limits, run worktree setup), each with its current value
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
the current `gh` account is not trusted, since Macatia would then ignore its own
implementation evidence. Git changes the repository (owner/name), base branch and
remote, each asked with its current value as the default and the detected one (gh
repository, default branch, Git remotes) shown when it differs; without `gh` or `git`
output, editing still works. After a repository change, the labels missing there are
offered for creation (Yes / Later), as `init` does, and created once the file is
written. Verification turns local gates,
CI checks and the final CI check on or off independently, and its summary reads
`local … · CI … · final CI …`; enabling one without the quality module and
manifest is accepted with a warning that `macatia check` fails until they exist.
Reviewed runs by default turns `review_by_default` on or off. Turning it on without a
review profile first asks for one, as Model profiles does for review; turning it off keeps
the review profile.
Keep the machine awake by default turns `caffeinate_by_default` on or off; it asks
for nothing else.
Child session limits changes `standard` or `reviewed` in `[child_session_limit]`, with
the current value as the default and the same validation as loading (an integer, at
least 1 for standard and 2 for reviewed). `init` writes the table with its defaults.
Run worktree setup sets `worktree_setup` when there is none (an empty answer goes back),
otherwise changes it, with the current command as the default, or removes it, which drops
the key from the file.
Save lists each changed key as `key: old → new`, writes the file, validates it and
suggests committing it, then `macatia check`; Save without changes, Quit without
saving and Ctrl-C write nothing. The file is rewritten in the layout `init` uses, so
comments added by hand are dropped (`configure` says so when it opens the file). When it
opens a file with defaulted settings (settings the file leaves out, which take their
default value), it lists them and points to `macatia update-config`, which writes them;
Save does not write them unless a value changes. It needs a terminal; without one, edit
the TOML directly. An invalid configuration is refused with the error: fix the file by
hand (a missing label key names the line to add); `macatia init --force` would start
over and lose its settings.
Unknown keys usually mean the file was written by a newer macatia (for example
committed from another machine): the error suggests `uv tool upgrade macatia --reinstall`.

## Add new settings: `update-config`

A newer macatia can add a setting. `macatia check` lists the defaulted
settings the file leaves out other than label roles (`review_by_default`,
`caffeinate_by_default`, each `child_session_limit` key, `verification.final_ci_check`) as information that
does not fail the check, and warns about defaulted label roles.
`macatia update-config [--config PATH]` writes each defaulted setting the file
leaves out, with its default value and the comment `init` writes next to it: `review_by_default` and `caffeinate_by_default`
after the last top-level setting, before the first table, each label role at
the end of `[labels]` with its default name, and a `[child_session_limit]` table
with `standard` and `reviewed` at the end of the file, or the limit a partial
table leaves out at the end of that table, without the table's comment, and
`final_ci_check = false` with its comment at the end of `[verification]`. A comment
block directly above the next table stays with that table. The rest of the file and its comments
stay as they are; `[models.review]`, `[models.backup]` and `worktree_setup` are never added. It lists
each added setting as `key = value`, and with nothing to add says so and writes
nothing. It refuses when the default name is already another role's label, when
the labels are not in a `[labels]` table, or when a partial `child_session_limit`
or a `verification` that leaves out `final_ci_check` is written as an inline table
or with dotted keys, and then writes nothing. It validates the
completed file before writing it and, like `check`, offers to create the labels
missing on GitHub (Yes / Later; without a terminal it only prints the command). Commit the file afterwards: a run needs a
clean working tree.

## Variant configurations

For a variant of an existing configuration, copy the file, then change the copy:

```sh
cp macatia.toml review.toml
macatia configure --config review.toml
macatia check --config review.toml
```

## Repository settings

- `repository` is the exact GitHub `owner/name` returned by `gh repo view`.
- `base_branch` is the protected branch Macatia never commits to; it only fast-forwards it
  from the remote before creating a run branch, never when switching to an existing one.
- `remote` is the Git remote Macatia pushes to.
- `trusted_authors` is the non-empty allowlist of trusted authors for parent
  issues and children. Include the GitHub account Macatia runs as so a restarted
  run can trust its own implementation evidence.
- `review_by_default` is optional (`false` when missing). `true` makes every
  run a reviewed run, in the configured `review_mode`, which requires
  `models.review`; `macatia run --no-review` overrides it for one run.
- `review_mode` is optional (`"child"` when missing): the review mode of every
  reviewed run, `"child"` for child review or `"final"` for final review; any
  other value is a configuration error. Child review adds a review session for
  each child; final review leaves children unreviewed, as in a standard run, and
  reviews the run target's whole work once every child is implemented (see
  [the loop](run.md)). It changes nothing for a standard run. `init` and
  `update-config` never write it, and `configure` keeps it as it is: set it by hand.

  ```toml
  review_mode = "final"
  ```
- `caffeinate_by_default` is optional (`false` when missing). `true` makes every
  run hold a sleep lock until it ends; `macatia run --no-caffeinate` overrides
  it for one run.
- `worktree_setup` is optional, with no default: a non-empty string without
  surrounding whitespace, the shell command that prepares a run worktree for
  `macatia run --worktree`, such as `pnpm install --frozen-lockfile` or
  `uv sync`, run in the run worktree on every `--worktree` run (see
  [Run worktree](run.md#preparing-the-run-worktree-worktree_setup)). Leaving it
  out prepares nothing.
  `init` never asks for it and `update-config` never adds it: set it with
  `configure` or by hand. `check` validates it without running it.

  ```toml
  worktree_setup = "uv sync"
  ```

- `[child_session_limit]` is an optional table with two optional integer keys:
  `standard` (5 when missing, at least 1) and `reviewed` (9 when missing, at
  least 2, so a run in child review can always reach its review session), the child
  session limit of a standard run and of a reviewed run in child review. A run
  uses the value of its mode: `reviewed` with `--review` or
  `review_by_default = true` in child review, `standard` with `--no-review`, in
  final review, or otherwise. The correction limit already bounds a child
  at 4 sessions in a standard run and 8 in a run in child review, so a value above
  that has no effect beyond room for a backup replay (see
  [the loop](run.md)); a lower value stops a child
  earlier. `macatia update-config` writes the table, or the limit it leaves out:

  ```toml
  [child_session_limit]
  standard = 5
  reviewed = 9
  ```

- `labels` maps Macatia's ten label roles to labels in the target
  repository: the triage labels `needs_triage`, `needs_info`, `ready_for_agent`,
  `ready_for_human`, and `wontfix`, the lifecycle labels `ready_for_agent`,
  `agent_working`, `human_review`, and `implemented`, the parent-only
  `work_started`, and the standalone marker `standalone`. `agent_working`
  (`agent-working` by default) marks the child a run is working on; only Macatia
  sets it. `work_started` (`work-started` by default) marks a parent on which
  Macatia has started work; it is informational
  only, and Macatia never reads it to decide anything: the first child cycle of a
  run adds it in place of `ready-for-agent`, parent completion removes it, and
  nothing else cleans it up (see [the loop](run.md)).
  `standalone` (`standalone` by default) is the standalone marker: it marks a
  standalone issue, one with no parent and no sub-issues, on which a run has
  started. Like `work_started`, it is neither a triage label nor a lifecycle
  label, Macatia never reads it to decide anything, and nothing removes it.
  A `macatia.toml` written before `work_started` or `standalone` existed may leave
  them out and gets their default names; `check` warns about it, and
  `macatia update-config` writes them. `init` and `check` offer to
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
values, a `worktree_setup` that is not a non-empty string, and child session limits that are not integers or are below their
minimum before starting an agent or changing GitHub state.
