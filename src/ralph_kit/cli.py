"""`ralph-kit` command line: dispatch to the run, the readiness check, init and configure."""

from __future__ import annotations

import argparse
import pathlib
import sys


USAGE = """usage: ralph-kit <command> [options]

commands:
  run [--review] [--caffeinate] [--config PATH] PARENT_ISSUE [RUN_BUDGET]
      Standard run: implement the children of PARENT_ISSUE, with at most RUN_BUDGET agent sessions.
      --review starts a reviewed run, which adds an independent review session per child.
      --caffeinate keeps the machine from idle-sleeping until the run ends.
  check [--review] [--config PATH]
      Read-only readiness check (configuration, CLIs, Git, GitHub, skills, quality policy).
  init [--force] [--config PATH]
      Write a configuration (default ./ralph.toml): detect repo, branch, remote, account and checks; ask for the rest.
      --force overwrites an existing configuration at PATH, and only that file.
  configure [--config PATH]
      Change sections of an existing configuration (default ./ralph.toml) from a menu; saves only what changed.

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

        caffeinate = "--caffeinate" in arguments
        arguments = [argument for argument in arguments if argument != "--caffeinate"]
        confirm = resume_with_arrows if sys.stdin.isatty() and sys.stdout.isatty() else None
        if not caffeinate:
            return loop.run(arguments, prog="ralph-kit run", confirm=confirm)
        from .keep_awake import keep_awake

        with keep_awake():
            return loop.run(arguments, prog="ralph-kit run", confirm=confirm)
    if command == "check":
        from . import check

        return check.main(arguments, prog="ralph-kit check")
    if command == "init":
        return init(arguments)
    if command == "configure":
        return configure(arguments)
    print(f"ralph-kit: unknown command {command!r}\n\n{USAGE}", end="", file=sys.stderr)
    return 2
