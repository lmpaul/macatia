"""`ralph-kit` command line: dispatch to the run, the readiness check, init, configure, update-config, close and reset."""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from .close import ClosableRunTarget
    from .loop_core import StartableRunTarget
    from .persistent_terminal import Continuation
    from .reset import ResettableRunTarget


USAGE = """usage: ralph-kit <command> [options]

commands:
  run [--review | --no-review] [--caffeinate | --no-caffeinate] [--tmux] [--config PATH] [ISSUE]
      Standard run: implement the children of a parent ISSUE, or a standalone ISSUE,
      giving each child at most 5 agent sessions (9 in a reviewed run).
      Without ISSUE, at a terminal, offers the startable run targets to choose from.
      --review starts a reviewed run, which adds an independent review session per child.
      --no-review starts a standard run, even when review_by_default = true in the configuration.
      --caffeinate keeps the machine from idle-sleeping until the run ends.
      --no-caffeinate lets the machine sleep, even when caffeinate_by_default = true in the configuration.
      --tmux asks, once the run target passes its startup checks, where the run continues:
      Restart in ralph-kit-<repository name>-<run target> (the default) restarts the run in that
      persistent terminal (tmux) and attaches to it; Continue here runs in this terminal;
      Stop changes nothing.
  check [--review] [--config PATH]
      Read-only readiness check (configuration, CLIs, Git, GitHub, skills, quality policy).
      --review also checks the review role, as review_by_default = true does.
  init [--force] [--config PATH]
      Write a configuration (default ./ralph.toml): detect repo, branch, remote, account and checks; ask for the rest.
      --force overwrites an existing configuration at PATH, and only that file.
  configure [--config PATH]
      Change sections of an existing configuration (default ./ralph.toml) from a menu; saves only what changed.
  update-config [--config PATH]
      Add each defaulted setting the configuration leaves out (review_by_default, caffeinate_by_default,
      label roles), with its default value and init's comment, keeping the rest of the file;
      then offer to create the labels missing on GitHub.
  close [--yes | --force] [--config PATH] [ISSUE]
      Closure: once every open child of a parent ISSUE is implemented and ISSUE is in human review,
      close the open children, then ISSUE, as completed, after one confirmation; a standalone ISSUE
      that is implemented and merged closes alone.
      Without ISSUE, at a terminal, offers the closable run targets to choose from.
      --yes closes without asking; without a terminal, close needs --yes.
      --force closes despite failed checks, shown as warnings, once you type ISSUE at a terminal;
      it needs ISSUE.
  reset [--yes] [--purge] [--config PATH] [ISSUE]
      Reset, after one confirmation: close the open cumulative draft PR of ISSUE, delete
      ralph-kit/issue-ISSUE on the remote and in this checkout, then label ready-for-agent again
      each child of ISSUE that is agent-working, implemented or in human-review, then ISSUE itself,
      each with a reset marker Ralph reads none of its earlier records past; ISSUE must be open
      with no merged child.
      Without ISSUE, at a terminal, offers the resettable run targets to choose from.
      --yes resets without asking; without a terminal, reset needs --yes.
      --purge deletes Ralph's comments on ISSUE and those children instead of leaving reset
      markers; a reset marker on ISSUE then counts as the trace of a run too.

Run every command from the target repository root. Configuration defaults to ./ralph.toml.
"""

def init(argv: list[str]) -> int:
    from . import init as setup
    from .check import arrow_select
    from .progress import spinner

    parser = argparse.ArgumentParser(prog="ralph-kit init", description="Write a configuration; ask for what cannot be detected.")
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path(setup.CONFIG_NAME), metavar="PATH")
    parser.add_argument("--force", action="store_true", help="overwrite an existing configuration at --config, and only that file")
    args = parser.parse_args(argv)  # Exits with status 2 on unknown arguments.
    interactive = sys.stdin.isatty()
    terminal = interactive and sys.stdout.isatty()
    prompter = setup.Prompter(interactive=interactive, select=arrow_select if terminal else None, spinner=spinner if terminal else None)
    try:
        setup.run(pathlib.Path.cwd().resolve(), prompter, force=args.force, config=args.config)
    except setup.InitAborted as error:
        print(f"\n{error}", file=sys.stderr)
        return 130
    except setup.InitError as error:
        print(f"❌ {error}", file=sys.stderr)
        return 1
    return 0


def configure(argv: list[str]) -> int:
    from . import configure as editor, init as setup
    from .check import arrow_select
    from .progress import spinner

    parser = argparse.ArgumentParser(prog="ralph-kit configure", description="Change sections of an existing configuration.")
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path(setup.CONFIG_NAME), metavar="PATH")
    args = parser.parse_args(argv)  # Exits with status 2 on unknown arguments.
    interactive = sys.stdin.isatty()
    terminal = interactive and sys.stdout.isatty()
    prompter = setup.Prompter(interactive=interactive, select=arrow_select if terminal else None, spinner=spinner if terminal else None)
    try:
        editor.run(pathlib.Path.cwd().resolve(), prompter, config=args.config)
    except editor.ConfigureAborted as error:
        print(f"\n{error}", file=sys.stderr)
        return 130
    except KeyboardInterrupt:  # Outside a question, before anything is written.
        print(f"\n{editor.CANCELLED}", file=sys.stderr)
        return 130
    except editor.ConfigureError as error:
        print(f"❌ {error}", file=sys.stderr)
        return 1
    return 0


def startup_choice_with_arrows(question: str, accept: str, *, accept_by_default: bool) -> bool:
    """Stop or `accept`, the default listed first; Ctrl-C raises KeyboardInterrupt, which the
    run counts as Stop."""
    from .check import arrow_select

    choices = [accept, "Stop"] if accept_by_default else ["Stop", accept]
    return arrow_select(question, [(choice, "") for choice in choices], choices[0]) == accept


def pick_run_target_with_arrows(
    question: str,
    targets: list[StartableRunTarget] | list[ClosableRunTarget] | list[ResettableRunTarget],
    preselected: int | None,
) -> int:
    """The number of the run target picked, `preselected` or the first one listed by default;
    Ctrl-C raises KeyboardInterrupt."""
    from .check import arrow_select

    numbers = {target.label: target.number for target in targets}
    default = next(
        (target.label for target in targets if target.number == preselected), targets[0].label
    )
    picked = arrow_select(question, [(label, "") for label in numbers], default)
    return numbers[picked]


def run_target_with_arrows(targets: list[StartableRunTarget], preselected: int | None) -> int:
    """The number of the startable run target picked, `preselected` or the first one listed
    by default; Ctrl-C raises KeyboardInterrupt, which the run counts as Stop."""
    return pick_run_target_with_arrows("Which run target should Ralph run?", targets, preselected)


def close_target_with_arrows(targets: list[ClosableRunTarget], preselected: int | None) -> int:
    """The number of the closable run target picked, `preselected` or the first one listed
    by default; Ctrl-C raises KeyboardInterrupt, which close counts as a cancellation."""
    return pick_run_target_with_arrows("Which run target should Ralph close?", targets, preselected)


def reset_target_with_arrows(targets: list[ResettableRunTarget], preselected: int | None) -> int:
    """The number of the resettable run target picked, `preselected` or the first one listed
    by default; Ctrl-C raises KeyboardInterrupt, which reset counts as a cancellation."""
    return pick_run_target_with_arrows("Which run target should Ralph reset?", targets, preselected)


def continuation_with_arrows(name: str) -> Continuation:
    """Where a `--tmux` run continues, Restart in `name` by default; Ctrl-C raises
    KeyboardInterrupt, which the run counts as Stop."""
    from .check import arrow_select
    from .persistent_terminal import QUESTION, continuation_choices

    choices = continuation_choices(name)
    answers = {choice.label: choice.answer for choice in choices}
    picked = arrow_select(
        QUESTION, [(choice.label, choice.description) for choice in choices], choices[0].label
    )
    return answers[picked]


def yes_no_with_arrows(question: str) -> bool:
    """No (the default) or Yes; Ctrl-C raises KeyboardInterrupt."""
    from .check import arrow_select

    return arrow_select(question, [("No", ""), ("Yes", "")], "No") == "Yes"


def main(argv: list[str] | None = None) -> int:
    # Kept for source checkouts run with an older interpreter; pip enforces it otherwise.
    if sys.version_info < (3, 11):  # noqa: UP036
        print("❌ ralph-kit requires Python 3.11 or newer.", file=sys.stderr)
        return 1
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(USAGE, end="")
        return 0 if argv else 2
    if argv[0] in {"-V", "--version"}:
        from . import __version__

        print(f"ralph-kit {__version__}")
        return 0

    sys.dont_write_bytecode = True
    command, arguments = argv[0], argv[1:]
    if command == "run":
        from . import loop

        terminal = sys.stdin.isatty() and sys.stdout.isatty()
        return loop.run(
            arguments,
            prog="ralph-kit run",
            confirm=startup_choice_with_arrows if terminal else None,
            choose_run_target=run_target_with_arrows if terminal else None,
            choose_continuation=continuation_with_arrows if terminal else None,
        )
    if command == "check":
        from . import check

        return check.main(arguments, prog="ralph-kit check")
    if command == "init":
        return init(arguments)
    if command == "configure":
        return configure(arguments)
    if command == "update-config":
        from . import update_config

        return update_config.main(arguments, prog="ralph-kit update-config")
    if command == "close":
        from . import close

        terminal = sys.stdin.isatty() and sys.stdout.isatty()
        return close.run(
            arguments,
            confirm=yes_no_with_arrows if terminal else None,
            type_answer=input if terminal else None,
            choose_run_target=close_target_with_arrows if terminal else None,
        )
    if command == "reset":
        from . import reset

        terminal = sys.stdin.isatty() and sys.stdout.isatty()
        return reset.run(
            arguments,
            confirm=yes_no_with_arrows if terminal else None,
            choose_run_target=reset_target_with_arrows if terminal else None,
        )
    print(f"ralph-kit: unknown command {command!r}\n\n{USAGE}", end="", file=sys.stderr)
    return 2
