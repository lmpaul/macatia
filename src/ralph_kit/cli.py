"""`ralph-kit` command line: dispatch to the run, the readiness check, init, configure and close."""

from __future__ import annotations

import argparse
import pathlib
import sys


USAGE = """usage: ralph-kit <command> [options]

commands:
  run [--review | --no-review] [--caffeinate | --no-caffeinate] [--config PATH] PARENT_ISSUE [RUN_BUDGET]
      Standard run: implement the children of PARENT_ISSUE, with at most RUN_BUDGET agent sessions.
      --review starts a reviewed run, which adds an independent review session per child.
      --no-review starts a standard run, even when review_by_default = true in the configuration.
      --caffeinate keeps the machine from idle-sleeping until the run ends.
      --no-caffeinate lets the machine sleep, even when caffeinate_by_default = true in the configuration.
  check [--review] [--config PATH]
      Read-only readiness check (configuration, CLIs, Git, GitHub, skills, quality policy).
      --review also checks the review role, as review_by_default = true does.
  init [--force] [--config PATH]
      Write a configuration (default ./ralph.toml): detect repo, branch, remote, account and checks; ask for the rest.
      --force overwrites an existing configuration at PATH, and only that file.
  configure [--config PATH]
      Change sections of an existing configuration (default ./ralph.toml) from a menu; saves only what changed.
  close [--yes | --force] [--config PATH] PARENT
      Parent closure: once every open child of PARENT is implemented and PARENT is in human review,
      close the open children, then PARENT, as completed, after one confirmation.
      --yes closes without asking; without a terminal, close needs --yes.
      --force closes despite failed checks, shown as warnings, once you type PARENT at a terminal.

Run every command from the target repository root. Configuration defaults to ./ralph.toml.
"""

def init(argv: list[str]) -> int:
    from . import init as setup
    from .check import arrow_select

    parser = argparse.ArgumentParser(prog="ralph-kit init", description="Write a configuration; ask for what cannot be detected.")
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path(setup.CONFIG_NAME), metavar="PATH")
    parser.add_argument("--force", action="store_true", help="overwrite an existing configuration at --config, and only that file")
    args = parser.parse_args(argv)  # Exits with status 2 on unknown arguments.
    interactive = sys.stdin.isatty()
    prompter = setup.Prompter(interactive=interactive, select=arrow_select if interactive and sys.stdout.isatty() else None)
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

    parser = argparse.ArgumentParser(prog="ralph-kit configure", description="Change sections of an existing configuration.")
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path(setup.CONFIG_NAME), metavar="PATH")
    args = parser.parse_args(argv)  # Exits with status 2 on unknown arguments.
    interactive = sys.stdin.isatty()
    prompter = setup.Prompter(interactive=interactive, select=arrow_select if interactive and sys.stdout.isatty() else None)
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


def resume_with_arrows(question: str) -> bool:
    """Stop (the default) or Resume; Ctrl-C raises KeyboardInterrupt, which the run counts as Stop."""
    from .check import arrow_select

    return arrow_select(question, [("Stop", ""), ("Resume", "")], "Stop") == "Resume"


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

        confirm = resume_with_arrows if sys.stdin.isatty() and sys.stdout.isatty() else None
        return loop.run(arguments, prog="ralph-kit run", confirm=confirm)
    if command == "check":
        from . import check

        return check.main(arguments, prog="ralph-kit check")
    if command == "init":
        return init(arguments)
    if command == "configure":
        return configure(arguments)
    if command == "close":
        from . import close

        terminal = sys.stdin.isatty() and sys.stdout.isatty()
        return close.run(
            arguments,
            confirm=yes_no_with_arrows if terminal else None,
            type_answer=input if terminal else None,
        )
    print(f"ralph-kit: unknown command {command!r}\n\n{USAGE}", end="", file=sys.stderr)
    return 2
