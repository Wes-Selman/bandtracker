"""
cli/main.py

BandTracker command-line interface — command router.

Dispatches to subcommand handlers in cli/commands/.
Each handler registers itself via add_subparser() and
exposes a run(args) function that returns an exit code.
"""

from __future__ import annotations

import argparse
import sys

from cli.commands import init as cmd_init
from cli.commands import snapshot as cmd_snapshot
from cli.commands import learn_noise as cmd_learn_noise
from cli.commands import watch as cmd_watch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bandtracker",
        description="Version control for GarageBand.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="bandtracker 0.1.0",
    )

    subparsers = parser.add_subparsers(
        title="commands",
        dest="command",
        metavar="<command>",
    )
    subparsers.required = True

    cmd_init.add_subparser(subparsers)
    cmd_snapshot.add_subparser(subparsers)
    cmd_learn_noise.add_subparser(subparsers)
    cmd_watch.add_subparser(subparsers)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
