"""`ralph-kit` command line: dispatch to the standard or reviewed run, the readiness check, and init."""

from __future__ import annotations

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

Run every command from the target repository root. Configuration defaults to ./ralph.toml.
"""

def init(argv: list[str]) -> int:
    from . import init as setup
    from .check import arrow_select

    force = False
    config = pathlib.Path(setup.CONFIG_NAME)
    unknown = []
    arguments = iter(argv)
    for argument in arguments:
        if argument == "--force":
            force = True
        elif argument == "--config" or argument.startswith("--config="):
            value = argument.partition("=")[2] if "=" in argument else next(arguments, "")
            if not value:
                print("ralph-kit init: --config needs a PATH", file=sys.stderr)
                return 2
            config = pathlib.Path(value)
        else:
            unknown.append(argument)
    if unknown:
        print(f"ralph-kit init: unknown arguments: {' '.join(unknown)}", file=sys.stderr)
        return 2
    interactive = sys.stdin.isatty()
    prompter = setup.Prompter(interactive=interactive, select=arrow_select if interactive and sys.stdout.isatty() else None)
    try:
        setup.run(pathlib.Path.cwd().resolve(), prompter, force=force, config=config)
    except setup.InitAborted as error:
        print(f"\n{error}", file=sys.stderr)
        return 130
    except setup.InitError as error:
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
        review = "--review" in arguments
        caffeinate = "--caffeinate" in arguments
        arguments = [argument for argument in arguments if argument not in {"--review", "--caffeinate"}]
        if review:
            from . import loop_review as loop
        else:
            from . import loop
        prog = "ralph-kit run" + (" --review" if review else "")
        confirm = resume_with_arrows if sys.stdin.isatty() and sys.stdout.isatty() else None
        if not caffeinate:
            return loop.run(arguments, prog=prog, confirm=confirm)
        from .keep_awake import keep_awake

        with keep_awake():
            return loop.run(arguments, prog=prog, confirm=confirm)
    if command == "check":
        from . import check

        return check.main(arguments, prog="ralph-kit check")
    if command == "init":
        return init(arguments)
    print(f"ralph-kit: unknown command {command!r}\n\n{USAGE}", end="", file=sys.stderr)
    return 2
