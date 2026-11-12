# Verification

`verification.local_checks` (local gates) and `verification.ci_checks` (CI
checks) are independent booleans:

| `local_checks` | `ci_checks` | Behaviour |
| --- | --- | --- |
| `true` | `true` | Run applicable local gates, then require exact-SHA CI. |
| `true` | `false` | Run applicable local gates and do not query CI. |
| `false` | `true` | Skip local gates and require exact-SHA CI. |
| `false` | `false` | Skip verification; implementation evidence explicitly records that it was disabled. A reviewed run still performs independent agent review. |

Use TOML booleans without quotes. These settings replace the former `mode` and
`local_gates` configuration keys; update custom configurations before running.

The last combination is an explicit low-assurance mode for repositories with no
CI and no local test commands. Macatia still pushes a draft pull request, verifies
that the completed commit is the published one, and leaves merge authority to a
human.

## Final CI check

`verification.final_ci_check` (a TOML boolean, `false` when absent) asks for a
**final CI check**: with `ci_checks = false`, each child is still pushed and the
cumulative draft PR created or reused, but the run does not wait for CI between
children. Once no runnable child is left, the run waits once for the required CI
checks of its final head, selected from every path changed since the first child
base of the run, before the end-of-run summary and marking the pull request
ready. With `ci_checks = true` it has no effect, and `macatia check` warns about
it. With `local_checks = false`, nothing verifies the children between them:
`macatia check` warns and a run prints, at start, a recommendation to set
`local_checks = true`, then proceeds. `macatia init` writes it with the value it
gives `ci_checks`; `macatia update-config` adds `final_ci_check = false` to a
configuration that leaves it out.

Each child's implementation evidence then says its local gates are green for the
exact head and that CI is verified at the end of the run; with local gates also
off, it says no automated check ran for the child.

Red CI starts a **final CI correction**: a correction session that belongs to the
run, not to any child. It runs on the correction model profile (or the backup
after an engine failure), never with a review session, and its prompt carries the
run target, the children implemented in this run, the sanitized CI diagnostic
(failed checks and verified job logs), and the **per-child CI history** below.
It must commit; its head then passes the enabled local gates and a new final CI
check. A local gate failure is repaired by
another final CI correction. A run starts at most three final CI corrections,
counted toward no child's child session limit or correction limit, and records
each in `run.json` under `final_ci_corrections`.

Every intermediate push still triggers CI on GitHub, so when the final CI check
first fails the run reads, for each child implemented in this run in order, the CI
state of its verified head: green, red, pending, cancelled, or missing (no required
check run, or a lookup that failed; a failed lookup never stops the run). The
first child whose verified head is red is named as a hint, not a certainty: a
child's CI run may have been cancelled by workflow concurrency or still be
pending, and the failure may come from two children together. The history and the
hint appear in every final CI correction prompt and in the handoff comment when
the run target goes to human review after a red final CI check.

Green CI adds a line to the end-of-run summary stating CI is green for the exact
final head and how many final CI corrections it took; the pull request is then
marked ready. Three final CI corrections exhausted, or a final CI correction that
makes no new commit or reports `blocked`, `failed`, or `needs_correction`, sends
the run target (the standalone issue itself, for a standalone run) to human review
with a handoff comment holding the last diagnostic. CI evidence that cannot be
obtained within the infrastructure retries or the CI wait's timeout does the same
without starting a final CI correction. In every case the children stay
implemented and the pull request stays a draft. A stopped, deadlocked, or
interrupted run makes no final CI check.

## Quality policy

The quality policy (quality module and manifest) is required whenever local
gates, CI checks, or the final CI check are enabled. It is not read when all are disabled.

Both kinds of run retain redacted session outputs and local-gate or CI command
evidence in the run diagnostics under `.macatia/diagnostics/<run-id>/`. Each new
agent session receives the issue and commit context plus paths to the previous
result and full command outputs. Failed CI jobs are matched to the checked commit
before their logs are passed to a correction session. A reviewed run also passes
this evidence to the reviewer.
