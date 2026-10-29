# ralph-kit

Ralph runs a loop of coding agents (Codex or Claude Code) over a parent GitHub
issue, one runnable child (a GitHub sub-issue) at a time, or over a standalone
issue on its own: implement, verify, correct, publish a draft PR. A human
reviews and merges. The project vocabulary is defined in
[GLOSSARY.md](GLOSSARY.md).

`ralph-kit` is installed once per machine and run from any repository. Each
repository only keeps its own `ralph.toml` (plus, when verification is enabled,
its quality module and manifest).

## Install

Requires Python 3.11+ (uv can provide it), Git, an authenticated `gh` 2.70.0 or
newer, and the CLI of each configured agent engine.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh        # once per machine
uv tool install git+https://github.com/lmpaul/ralph-kit # installs `ralph-kit` on PATH
uv tool upgrade ralph-kit --reinstall                   # pick up the latest commit
```

Pin a release with `git+https://github.com/lmpaul/ralph-kit@v0.6.3`.

## Use in a repository

From the repository root:

```sh
ralph-kit init                 # writes ./ralph.toml (detects what it can, asks the rest)
ralph-kit init --config b.toml  # writes ./b.toml instead, for a second configuration
ralph-kit configure            # change sections of ./ralph.toml from a menu
ralph-kit update-config        # add the defaulted settings ./ralph.toml leaves out, then their GitHub labels
ralph-kit check                # readiness check (offers to create or recolour labels)
ralph-kit check --review       # also checks the review role (implied by review_by_default = true)
ralph-kit run                  # at a terminal, pick among the startable run targets
ralph-kit run 123              # standard run: implement the children of parent issue #123
ralph-kit run --review 123     # reviewed run: adds an independent review session per child
ralph-kit run --no-review 123  # standard run, even when review_by_default = true
ralph-kit run --caffeinate 123 # keep the machine awake until the run ends
ralph-kit run --no-caffeinate 123 # let it sleep, even when caffeinate_by_default = true
ralph-kit run --tmux 123       # offer to restart the run in a persistent terminal (tmux)
ralph-kit close                # at a terminal, pick among the closable run targets
ralph-kit close 123            # closure: close #123's children, then #123, or standalone #123 alone (asks first)
ralph-kit close --yes 123      # same, without the confirmation
ralph-kit close --force 123    # close despite failed checks, after typing 123
ralph-kit reset                # at a terminal, pick among the resettable run targets
ralph-kit reset 123            # reset: make #123 and its children ready for an agent again (asks first)
ralph-kit reset --yes 123      # same, without the confirmation
ralph-kit reset --purge 123    # same, deleting Ralph's comments instead of leaving reset markers
```

## Documentation

See [docs/](docs/README.md): [configuration](docs/configuration.md),
[readiness check](docs/check.md), [the loop](docs/run.md),
[Codex and Claude Code](docs/engines.md), [verification](docs/verification.md),
[incomplete issue work](docs/incomplete-work.md), [closure](docs/close.md)
and [reset](docs/reset.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT, see [LICENSE](LICENSE).
