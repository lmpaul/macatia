"""`ralph-kit` command line: dispatch to the loops, the readiness check, and init."""

from __future__ import annotations

import pathlib
import sys

USAGE = """usage: ralph-kit <command> [options]

commands:
  run [--review] [--config PATH] PARENT_ISSUE [AGENT_BUDGET]
      Implement the sub-issues of PARENT_ISSUE. --review adds the independent review session.
  check [--review] [--config PATH]
      Read-only readiness check (configuration, CLIs, Git, GitHub, skills, quality policy).
  init [--force]
      Write a ralph.toml template at the repository root.

Run every command from the target repository root. Configuration defaults to ./ralph.toml.
"""

CONFIG_TEMPLATE = pathlib.Path(__file__).with_name("ralph.template.toml")


def init(argv: list[str]) -> int:
    force = "--force" in argv
    if unknown := [argument for argument in argv if argument != "--force"]:
        print(f"ralph-kit init: unknown arguments: {' '.join(unknown)}", file=sys.stderr)
        return 2
    target = pathlib.Path.cwd() / "ralph.toml"
    if target.exists() and not force:
        print(f"ralph-kit init: {target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    target.write_text(CONFIG_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"✅ Wrote {target}. Replace the placeholders, then run `ralph-kit check`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
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
        arguments = [argument for argument in arguments if argument != "--review"]
        if review:
            from . import loop_review as loop
        else:
            from . import loop
        return loop.run(arguments, prog="ralph-kit run" + (" --review" if review else ""))
    if command == "check":
        from . import check

        return check.main(arguments, prog="ralph-kit check")
    if command == "init":
        return init(arguments)
    print(f"ralph-kit: unknown command {command!r}\n\n{USAGE}", end="", file=sys.stderr)
    return 2
