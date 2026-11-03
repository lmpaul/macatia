# Develop macatia

```sh
git clone git@github.com:lmpaul/macatia.git && cd macatia
uv tool install -e .                          # `macatia` now runs this checkout
uv run python -m unittest discover -s tests   # offline tests (~35 s), with dependencies
```

Bump `__version__` in `src/macatia/__init__.py` with every change to `macatia.toml`'s
keys, so `macatia --version` tells whether a machine can read a newer file.

The default suite is offline: it never starts `macatia run` as a subprocess,
launches a real agent, or calls GitHub. Each rule is tested at one level:

- **Child cycle rules** (correction limit, review session order, handoff on
  missing commits, child session limit checks inside a cycle):
  `tests/test_child_cycle.py`,
  in memory through the child cycle adapters.
- **Run rules** (child selection, blockers, deadlock, recovery, publication,
  parent completion, infrastructure retries, startup and trust):
  `tests/test_run.py`, a whole run in memory through injected run adapters.
  `tests/run_support.py` provides a GitHub fake, scripted sessions or engine
  processes, and a real temporary Git repository with a bare `origin`.
- **Engine translation** (the CLI command built for each session, and the
  parsing of each engine's final result): `tests/test_agent.py`, pure functions
  against outputs recorded from the real CLIs in `tests/fixtures/engines/`.
- **Readiness check** (including the engine flag check): `tests/test_check.py`,
  with injected executable lookup and command runner. `tests/test_init.py`
  covers `macatia init` the same way, `tests/test_configure.py` covers
  `macatia configure`, and `tests/test_update_config.py` covers
  `macatia update-config`.
- **Closure** (every check, the confirmation, the writes and resuming
  them): `tests/test_close.py`, the close command's entry point in memory with
  the GitHub fake and a scripted confirmer.
- **Reset** (every refusal, the plan, the confirmation, the cumulative draft PR,
  the run worktree, the run branch, the labels, reset markers and recap, the
  purge, and resuming them):
  `tests/test_reset.py`, the reset command's entry point with the GitHub fake, a
  scripted confirmer and a temporary checkout with a bare `origin`.
- **Live suite**: the only proof of compatibility with the real `gh`, `codex`,
  and `claude`: `tests/test_live.py`, skipped unless `MACATIA_LIVE=1` (see below).

## Live suite

Run it before a release and after upgrading `gh`, `codex` or `claude`. It needs
an authenticated `gh` and engine CLIs, and spends a few tokens, so it is not part
of the default suite, local gates or CI:

```sh
MACATIA_LIVE=1 uv run python -m unittest discover -s tests -p test_live.py -v
```

It makes read-only `gh` calls of each shape a run or `close` parses (repository,
children, blockers, issue details, open pull requests, check runs, the Actions job,
job log and check run behind a check, the compare of the default branch with
its head, and the merged pull requests of a merged pull request's head commit)
through Macatia's GitHub adapter, against the
children of parent issue #5 in this repository (`MACATIA_LIVE_PARENT` selects another
parent issue with children and blockers) and the check runs of its default branch
head. It then starts one Claude Code and one Codex session in a scratch Git
repository, with a trivial prompt, Macatia's real engine command and output schema,
and checks that Macatia's result parsing accepts the result. A failed session
reports the engine's exit code, stderr and stdout. `MACATIA_LIVE_CLAUDE_MODEL`
(default `haiku`) and `MACATIA_LIVE_CODEX_MODEL` (default `gpt-5.6-luna`) select the
models. Without `MACATIA_LIVE=1` its tests are skipped; `-v` shows the skip reason.

To refresh the recorded engine outputs in `tests/fixtures/engines/` (after an
engine upgrade), add `MACATIA_LIVE_REFRESH=1`. Each passing session replaces its
engine's output and records the CLI version, model and date in `recorded.json`;
commit the changed fixtures only when the whole live suite and the default suite
pass:

```sh
MACATIA_LIVE=1 MACATIA_LIVE_REFRESH=1 uv run python -m unittest discover -s tests -p test_live.py -v
```
