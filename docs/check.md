# Readiness check

Before launching, from the target repository root on each machine (including a VPS):

```sh
macatia check
# For a reviewed run:
macatia check --review
```

Commit the prepared files first. The readiness check validates
TOML and placeholders, Python and required CLI availability (and the minimum `gh` version), Git branch/remote
and clean worktree, `.macatia/` ignored by Git (any ignore source counts; the check
never edits `.gitignore`), GitHub access/repository/trusted account/write permission/labels,
the packaged result schema, the implement skill for each engine, the flags Macatia passes
to each configured engine (listed in the engine's own help, so an update that drops one
blocks before a run), and the enabled quality policy. A configured backup's engine counts
as a configured engine (CLI, implement skill, and flags), and its model name is checked
for placeholders like the others.
It inspects local gate commands and CI check names without executing them.
Disabled verification may keep placeholder entries; when both are disabled the
quality policy is not loaded.
Results are grouped by section with what was found; each failure shows the fix
(often a command to run), and checks that depend on a missing tool are reported
as not verified instead of failing. Colors are dropped when the output is not a
terminal (or with `NO_COLOR`).
Settings the configuration leaves out and that take their default value are
listed as information (`ℹ`), pointing to `macatia update-config`; they never fail
the check. Defaulted label roles are a warning instead, since the label may be
missing on GitHub.
Labels whose colour differs from their label role's fixed colour are a warning,
with the `gh label edit … --color …` commands as the fix.
A nonzero exit code means a blocking check failed. In a terminal, missing labels
are offered for creation after the report (Yes / Later), then labels with the
wrong colour are offered for recolouring (colour only, never name or description);
those are the only GitHub writes. Engines only print their help; no agent session,
project check, CI run, push, issue or PR mutation occurs. The configured
quality module is imported: it must be trusted and have no import-time side effects.

A successful readiness check does not validate agent authentication/model access,
actual skill discovery or transitive dependencies, Git push credentials, project
dependencies and services, test results, actual CI workflow names, or the readiness
of the parent issue, its children and their blockers. Those remain the user's
responsibility, along with accepting the agents' permission-bypass options and
reviewing the resulting PR before merge.
