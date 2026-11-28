# Codex and Claude Code

Each model profile accepts `engine = "codex"` or `engine = "claude"`.
Omitting `engine` defaults to Codex, preserving existing configurations. Choose
an engine separately for implementation, independent review, and corrections;
a standard run uses only the implementation and correction profiles.
Use a model name supported by the selected CLI and your account.

For example, a backup on the other engine:

```toml
[models.backup]
engine = "codex"
name = "gpt-5.6-sol"
display_name = "Sol"
reasoning_effort = "medium"
```

For example, to implement and correct with Claude while retaining the existing
Codex reviewer, change the implementation and correction profiles:

```toml
[models.implementation]
engine = "claude"
name = "sonnet"
display_name = "Claude"
reasoning_effort = "medium"

[models.correction]
engine = "claude"
name = "sonnet"
display_name = "Claude correction"
reasoning_effort = "medium"
```

Python 3.11+, Git, authenticated `gh` 2.70.0 or newer, and the authenticated CLI for each selected
engine must be available on `PATH`. Project test tools are still required.
Claude efforts are `low`, `medium`, `high`, `xhigh`, or `max`; the selected model
may support only a subset. Codex retains its existing effort options. Model and
account availability are checked by the respective CLI at session start.

Both kinds of run use `macatia/agent.py` for engine-specific commands and result
normalization. Codex writes the schema result to a file; Claude runs in print
mode and returns it in `structured_output`. The same Macatia contract and Git
checks apply to both. Session outputs remain in the run diagnostics with the
engine and return code.

## Engine failures and the backup

An **engine failure** is a session that ends without a valid result, whatever
the cause (quota, rate limit, expired login, outage, unknown model): a Claude
Code error envelope, Claude Code output that is not valid JSON or has no
structured result, a missing Codex result file, a result that breaks the Macatia
contract, or a non-zero exit without a `committed` outcome. A non-zero exit with
a valid `committed` result is accepted. Agent outcomes (`failed`, `blocked`,
`needs_correction`, `no_changes`), rejected Git effects, and interruptions are
not engine failures.

Without `models.backup`, an engine failure stops the run safely; during the final
review, it or an engine failure of the backup also hands the run target to human
review (see [The final review stage](run.md#the-final-review-stage)). With one, Macatia
drops the failed session's commits and uncommitted files, so they are never
pushed, and replays the same session with the backup from the head the failed
session started from. The backup then runs every later session of the run,
whatever its role; the console announces the switch with the failed engine, its
exit detail, and the backup, confirms it once the replay returns a result, and
later session lines show the backup's engine and profile. The run diagnostics
keep the failed session and record the switch in `run.json`. The run summary, in
the terminal and on the parent issue and the cumulative draft PR, gains a line
such as `- backup: Claude Code Opus - High took over after a Codex engine failure
(review session, child #12)`, kept when the backup failed too, and has no such
line without an engine failure. The failed session and its replay both count
toward the child's session limit; when the failed session was the last one the
limit allowed, no switch happens: the console and the run summary report the
engine failure alone, such as `- engine failure: Codex in the review session
for child #12`, and the child goes to human review as for any exhausted child
session limit. A failed correction and its replay count as one
correction. An engine failure of the backup stops the run, naming both failures;
there is no second backup and no switch back. The next run starts on the regular
profiles again.

For Claude, Macatia invokes `/implement` directly and explicitly instructs the
agent to read `AGENTS.md`. Repo skills are exposed through individual relative
symlinks in `.claude/skills/`, pointing to `.agents/skills/`. Keep these links
available for `implement` and its dependencies. Each agent session starts
fresh.

The existing unattended permissions policy is retained: Codex uses
`--dangerously-bypass-approvals-and-sandbox`; Claude uses
`--dangerously-skip-permissions`. Both agents can execute commands and modify
files. Run in a checkout and environment appropriate for that access. Macatia
still owns publishing and a human owns merge.

CLI references: [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)
and [Claude Code programmatic usage](https://code.claude.com/docs/en/headless).
