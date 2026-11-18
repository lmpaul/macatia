# macatia

A coding agent handles one issue well. Hand it ten and it loses the thread:
skipped tests, tangled commits, work you can't tell is finished. Macatia is the
loop around the agent: one fresh session per sub-issue, your tests and CI as
the judge, a correction session when they fail, and a stop that hands the issue
back to you when it can't converge. Nothing merges without a human.

🇫🇷 [Video introduction, in French](https://www.youtube.com/watch?v=-XvVbPuDPrM):
what Macatia does and the ideas it is built on.

Macatia was called ralph-kit until 0.7.0, after the "Ralph" loop technique of
running a coding agent over and over against the same plan.

Macatia is built around [Matt Pocock's skills](https://github.com/mattpocock/skills).
Their workflow takes an idea through `/grill-with-docs` and `/to-spec`, and
`/to-tickets` splits the spec into GitHub sub-issues labelled `ready-for-agent`,
with their blocking links. From there, the workflow has you run `/implement` on
each ticket yourself, clearing the context between tickets. Macatia does that part
for you, unattended, and checks each result before moving on.

## How it works

`macatia run <parent>` takes the parent's `ready-for-agent` children in turn,
skipping any whose blockers are not done yet. Issues written by hand work as well
as tickets from `/to-tickets`, and a small change can stay a standalone issue with
no sub-issues. For each child, Macatia:

1. starts a fresh Claude Code or Codex session that runs `/implement` on it;
2. runs your local gates, the test and lint commands you configure, and starts a
   correction session when they fail;
3. pushes to the run branch, updates a single draft pull request for the whole
   parent, and waits for CI, correcting again when it fails;
4. labels the child `implemented` and moves on to the next one.

A child still failing after three corrections, or that the agent reports as
blocked, is labelled `human-review` and the run stops, its commits left on the
branch for you to inspect. When every child is implemented, the parent goes to
`human-review`: you review the draft pull request, merge it, and
`macatia close <parent>` closes the issues.

## Install

You need Python 3.11+ (uv can provide it), Git, an authenticated `gh` 2.70.0 or
newer, and Claude Code or Codex.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh        # once per machine
uv tool install git+https://github.com/lmpaul/macatia # installs `macatia` on PATH
uv tool upgrade macatia --reinstall                   # pick up the latest commit
```

Pin a release with `git+https://github.com/lmpaul/macatia@v0.8.0`.

Each repository Macatia works on also needs an `implement` skill checked in, under
`.claude/skills/` for Claude Code or `.agents/skills/` for Codex. See
[mattpocock/skills](https://github.com/mattpocock/skills) to install his into the
repository.

## Quickstart

From the root of the repository you want Macatia to work on:

```sh
macatia init        # writes macatia.toml: detects what it can, asks the rest
macatia check       # readiness check; offers to create missing labels
git add macatia.toml .gitignore && git commit -m "Configure macatia"
macatia run 123     # works through the ready children of parent issue #123
```

`init` asks which engine, model and reasoning effort each session uses, and which
GitHub accounts are trusted authors: Macatia only acts on issues they wrote. The
run starts on a clean worktree and offers to create the run branch
`macatia/issue-123`. Add `--review` for an independent review session on each
child before it is published. Once you have merged the draft pull request,
`macatia close 123` closes the children and the parent.

Local gates and CI checks need a quality module and manifest
(`.github/scripts/quality.py` and `.github/quality-gates.json`). Without them,
`init` turns both off and Macatia checks nothing beyond the agent's own result; see
[verification](docs/verification.md).

## Safety

Macatia runs agents unattended, so it turns their approval prompts off: Claude Code
gets `--dangerously-skip-permissions` and Codex
`--dangerously-bypass-approvals-and-sandbox`. Both can run any command and change
any file the session can reach. Run Macatia in a checkout and on a machine where
that is acceptable, such as a dedicated clone, a VM, a VPS or a container.

What it keeps under control:

- it only acts on issues written by a trusted author, so a stranger's issue on a
  public repository cannot steer an agent;
- it never commits to the base branch, only to the run branch, and it never
  merges: a human merges the draft pull request.

## Commands

| Command | What it does |
| --- | --- |
| `macatia init` | Writes `macatia.toml`, detecting what it can and asking the rest. |
| `macatia configure` | Changes sections of an existing `macatia.toml` from a menu. |
| `macatia update-config` | Adds the settings a newer macatia introduced, with their defaults. |
| `macatia check` | Checks that the machine, repository and GitHub are ready for a run. |
| `macatia run [ISSUE]` | Works through a parent's ready children, or a standalone issue. |
| `macatia close [ISSUE]` | Once the work is merged, closes the children, then the parent. |
| `macatia reset [ISSUE]` | Makes an issue and its children ready to run again from scratch. |

Without `ISSUE`, at a terminal, `run`, `close` and `reset` list the issues they
can act on. `run` also takes `--review`, `--caffeinate` (keep the machine awake),
`--tmux` (continue in a persistent terminal) and `--worktree` (run in a separate
run worktree, away from your own checkout). Every command takes
`--config PATH` to use another configuration file.

## Documentation

The [documentation](docs/README.md) covers each command and setting in detail:
[configuration](docs/configuration.md), [readiness check](docs/check.md),
[the loop](docs/run.md), [Codex and Claude Code](docs/engines.md),
[verification](docs/verification.md), [incomplete issue work](docs/incomplete-work.md),
[closure](docs/close.md) and [reset](docs/reset.md). The terms Macatia uses in its
output, comments and docs are defined in [GLOSSARY.md](GLOSSARY.md).

## Contributing

Macatia is shared as is: I use it every day and keep it shaped around my own
workflow, so I don't take pull requests or feature requests. Fork it and make it
yours. [DEVELOPMENT.md](DEVELOPMENT.md) explains how to work on a checkout and
run its test suites.

## License

[MIT](LICENSE) © PLM STUDIO

The skills under `.agents/skills/` and `.claude/skills/` come from
[mattpocock/skills](https://github.com/mattpocock/skills), MIT © Matt Pocock.
