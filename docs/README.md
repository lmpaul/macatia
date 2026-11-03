# macatia documentation

## User guide

- [Configuration](configuration.md): `init`, `configure`, `update-config` and every `macatia.toml` setting.
- [Readiness check](check.md): `check`, before each run.
- [The loop](run.md): `run` and `run --review`, the run branch, `--caffeinate` and `--tmux`.
- [Codex and Claude Code](engines.md): engines, engine failures and the backup.
- [Verification](verification.md): local gates and CI checks.
- [Incomplete issue work](incomplete-work.md): session outcomes, handoffs and interruptions.
- [Closure](close.md): `close`, once the work is merged.
- [Reset](reset.md): `reset`, to start a run target over.

The project vocabulary is defined in [GLOSSARY.md](../GLOSSARY.md).

## Project internals

- [Architecture decision records](adr/)
- [Agent skill configuration](agents/): issue tracker, triage labels and domain docs.
- [Contributing](../CONTRIBUTING.md): developing macatia and its test suites.
